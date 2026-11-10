/**
 * Update E2E: a real 0.0.4 install (built from the 0.0.4 commit with its own installer) that already has its
 * owner.json is updated to this version with `hokma update --from <local package>`; then the real panel opens.
 * Checks: the migration created hokma.db at the latest schema, the profile took the names of owner.json, and the
 * panel never asks the first-run steps again: only what is missing (here Claude, signed out), or nothing at all.
 * Needs network (the installer and the update download the runtime) and `git`, `tar`, `bash`.
 *
 *   node e2e/update.e2e.mjs [es|en]        (both locales when omitted)
 *   HOKMA_E2E_BASE_COMMIT=<commit>         the 0.0.4 state (default 36fc079)
 */
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { buildDashboard, free, loadPlaywright, repo, sleep, translator, until } from "./lib.mjs";

const BASE_COMMIT = process.env.HOKMA_E2E_BASE_COMMIT || "36fc079";
const OWNER = "Ana Update";
const FUND = "Fondo Update";
const SKIP = new Set([".git", "node_modules", ".venv", "__pycache__", ".pytest_cache", "dist"]);

function sh(cmd, args, env, cwd) {
  const r = spawnSync(cmd, args, { env, cwd, encoding: "utf8", timeout: 900000 });
  return { code: r.status, out: (r.stdout ?? "") + (r.stderr ?? "") };
}

function cleanEnv(home, extra = {}) {
  const env = {};
  for (const [k, v] of Object.entries(process.env)) {
    if (/^(HOKMA_|UV_|CLAUDE_CONFIG|XDG_)/.test(k) || k === "PYTHONPATH" || k === "PYTHONHOME") continue;
    env[k] = v;
  }
  return { ...env, HOME: home, SHELL: "/bin/bash", ...extra };
}

function fakeClaude(bin, signedIn) {
  fs.mkdirSync(bin, { recursive: true });
  const reply = signedIn ? '{"loggedIn": true, "subscriptionType": "pro"}' : '{"loggedIn": false}';
  fs.writeFileSync(
    path.join(bin, "claude"),
    `#!/usr/bin/env python3\nimport sys\nif sys.argv[1:3] == ["auth", "status"]:\n    print('${reply}')\nelif sys.argv[1:2] == ["--version"]:\n    print("2.0.0 (Claude Code)")\nelse:\n    sys.exit(1)\n`,
  );
  fs.chmodSync(path.join(bin, "claude"), 0o755);
}

/** The 0.0.4 checkout, installed with its own installer under a temporary HOME. */
function installBase(root, home, locale) {
  const src = path.join(root, "base-src");
  fs.mkdirSync(src, { recursive: true });
  const archive = spawnSync("git", ["-C", repo, "archive", BASE_COMMIT], { maxBuffer: 1 << 30 });
  assert.equal(archive.status, 0, `git archive ${BASE_COMMIT} failed`);
  const untar = spawnSync("tar", ["-x", "-C", src], { input: archive.stdout });
  assert.equal(untar.status, 0, "could not unpack the base commit");
  const r = sh("bash", [path.join(src, "installer/install.sh"), "--lang", locale, "--no-doctor"], cleanEnv(home), src);
  assert.equal(r.code, 0, `base install failed:\n${r.out}`);
}

/** A release package of THIS checkout, with the dashboard freshly built, plus its manifest. */
function buildPackage(root, version) {
  const dir = path.join(root, "pkg", `hokma-${version}`);
  fs.cpSync(repo, dir, {
    recursive: true,
    filter: (source) => {
      const rel = path.relative(repo, source);
      return !rel.split(path.sep).some((part) => SKIP.has(part)) && !rel.endsWith(".egg-info");
    },
  });
  fs.cpSync(buildDashboard(), path.join(dir, "dashboard/dist"), { recursive: true });
  const file = path.join(root, `hokma-${version}.tar.gz`);
  const r = sh("tar", ["-czf", file, "-C", path.join(root, "pkg"), `hokma-${version}`], process.env);
  assert.equal(r.code, 0, r.out);
  const manifest = path.join(root, `manifest-${version}.json`);
  fs.writeFileSync(manifest, JSON.stringify({ version, url: path.basename(file), sha256: crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex") }));
  return manifest;
}

function sqlite(db, query) {
  const code = `import sqlite3,sys,json\nc=sqlite3.connect(sys.argv[1])\nprint(json.dumps(c.execute(sys.argv[2]).fetchall()))`;
  const r = spawnSync("python3", ["-I", "-c", code, db, query], { encoding: "utf8" });
  assert.equal(r.status, 0, r.stderr);
  return JSON.parse(r.stdout);
}

async function startApp(launcher, env, port) {
  const child = spawn(launcher, ["start", "--no-browser", "--port", String(port)], { env, stdio: ["ignore", "pipe", "pipe"] });
  let out = "";
  child.stdout.on("data", (d) => (out += d));
  child.stderr.on("data", (d) => (out += d));
  const token = await until(() => /#t=([\w-]+)/.exec(out)?.[1], "the session token of the updated app", 60000);
  const once = /#t=[\w-]+&o=([\w-]+)/.exec(out)?.[1];
  assert.ok(once, "the start link carries the one-time code of the owner registration");
  const base = `http://127.0.0.1:${port}/`;
  await until(async () => (await fetch(`${base}health`)).ok, "the updated app health");
  const api = async (p) => (await fetch(`${base}${p.replace(/^\//, "")}`, { headers: { "X-Hokma-Token": token } })).json();
  return {
    base,
    token,
    once,
    api,
    async stop() {
      child.kill("SIGTERM");
      await sleep(400);
      if (child.exitCode === null) child.kill("SIGKILL");
    },
  };
}

const locales = process.argv[2] ? [process.argv[2]] : ["es", "en"];
const { chromium } = loadPlaywright();
let failed = false;
for (const locale of locales) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), `hokma-update-e2e-${locale}-`));
  const home = path.join(root, "home");
  fs.mkdirSync(home);
  const data = path.join(home, ".local/share/hokma");
  const work = path.join(home, "Hokma");
  const launcher = path.join(home, ".local/bin/hokma");
  const { msg } = translator(locale);
  let app;
  let browser;
  try {
    // (a) a real 0.0.4 install that already has its names, as 0.0.4 left them
    installBase(root, home, locale);
    fs.writeFileSync(path.join(data, "owner.json"), JSON.stringify({ owner_name: OWNER, fund_name: FUND }, null, 1) + "\n");
    assert.equal(sh(launcher, ["version"], cleanEnv(home)).out.trim(), "hokma 0.0.4");
    assert.ok(!fs.existsSync(path.join(data, "hokma.db")), "0.0.4 has no database");
    const slotBefore = fs.readFileSync(path.join(data, "current"), "utf8").trim();

    // (b) the update, from a local package of this version
    const manifest = buildPackage(root, versionOfRepo());
    const updated = sh(launcher, ["update", "--from", manifest], cleanEnv(home));
    assert.equal(updated.code, 0, `update failed:\n${updated.out}`);
    assert.match(updated.out, /0\.0\.4/);
    assert.equal(sh(launcher, ["version"], cleanEnv(home)).out.trim(), `hokma ${versionOfRepo()}`);
    assert.notEqual(fs.readFileSync(path.join(data, "current"), "utf8").trim(), slotBefore, "the pointer moved to the other slot");

    // (c1) the migration ran in the update: base created at the latest schema, profile from owner.json, not completed
    const db = path.join(data, "hokma.db");
    assert.ok(fs.existsSync(db), "the update created hokma.db");
    assert.equal(sqlite(db, "SELECT MAX(version) FROM schema_version")[0][0], 5, "schema 5");
    const [[ownerName, fundName, completedAt]] = sqlite(db, "SELECT owner_name, fund_name, setup_completed_at FROM profile");
    assert.equal(ownerName, OWNER);
    assert.equal(fundName, FUND);
    assert.equal(completedAt, null, "an update does not mark the setup as done by itself");
    assert.ok(fs.existsSync(path.join(work, ".git")) && fs.existsSync(path.join(work, "CLAUDE.md")), "the folder survived");

    browser = await chromium.launch({ headless: true, ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}) });
    const openPanel = async (url, token, once) => {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 940 } });
      await ctx.addInitScript(([tk, loc, o]) => {
        localStorage.setItem("hokma.locale", loc);
        // first load of the tab: the token comes the way a handoff gives it (window.name, never a storage); the start
        // link carries the one-time code of the owner registration, which the app takes and clears from the bar
        if (!window.name && window.location.protocol.startsWith("http")) {
          const hp = new URLSearchParams(window.location.hash.slice(1));                          // the address carries #/chat/... now
        hp.delete("t");
        hp.delete("o");
        const u = window.location.pathname + window.location.search + (hp.toString() ? "#" + hp.toString() : "");
          window.name = "hokma-handoff:" + JSON.stringify({ o: window.location.origin, u, t: tk });
          if (o && !window.location.hash) window.history.replaceState(null, "", "#o=" + o);
        }
      }, [token, locale, once]);
      const page = await ctx.newPage();
      const errors = [];
      page.on("pageerror", (e) => errors.push(e.message));
      await page.goto(url);
      return { page, errors, close: () => ctx.close() };
    };

    // (c2) Claude signed out: the panel asks only for Claude, then Done. Never the folder, database or name.
    fakeClaude(path.join(root, "bin-out"), false);
    const port = await free();
    app = await startApp(launcher, cleanEnv(home, { PATH: `${path.join(root, "bin-out")}:${process.env.PATH}` }), port);
    let view = await openPanel(app.base, app.token, app.once);
    const dialog = view.page.getByRole("dialog", { name: msg("firstrun.dialog") });
    await dialog.waitFor();
    await dialog.getByText(msg("firstrun.returning")).waitFor();
    const steps = await dialog.getByLabel(msg("firstrun.steps")).innerText();
    assert.ok(steps.includes(msg("firstrun.step.claude")) && steps.includes(msg("firstrun.step.done")), `steps: ${steps}`);
    for (const gone of ["folder", "database", "name"]) assert.ok(!steps.includes(msg(`firstrun.step.${gone}`)), `${gone} must not be asked: ${steps}`);
    await dialog.getByRole("heading", { name: msg("firstrun.claude.title") }).waitFor();
    assert.equal((await app.api("/api/setup")).completed, false);
    assert.equal((await app.api("/api/setup")).owner.owner_name, OWNER);
    assert.equal(view.errors.length, 0, view.errors.join(" | "));
    await view.close();
    await app.stop();

    // (c3) Claude signed in: nothing is missing, the panel goes straight in and the setup closes by itself
    fakeClaude(path.join(root, "bin-in"), true);
    app = await startApp(launcher, cleanEnv(home, { PATH: `${path.join(root, "bin-in")}:${process.env.PATH}` }), await free());
    view = await openPanel(app.base, app.token, app.once);
    await view.page.locator("aside").first().getByText(FUND).first().waitFor();
    await until(async () => (await app.api("/api/setup")).completed === true, "the setup closed by itself");
    assert.equal(await view.page.getByRole("dialog", { name: msg("firstrun.dialog") }).count(), 0, "no first-run modal");
    const setup = await app.api("/api/setup");
    assert.equal(setup.profile.owner_name, OWNER);
    assert.equal(setup.profile.fund_name, FUND);
    assert.equal(setup.profile.database_engine, "sqlite");
    assert.equal(view.errors.length, 0, view.errors.join(" | "));
    await view.close();
    console.log(`ok update ${locale}`);
  } catch (error) {
    failed = true;
    console.error(`FAIL update ${locale}:`, error.stack || error);
  } finally {
    await browser?.close();
    await app?.stop();
    fs.rmSync(root, { recursive: true, force: true });
  }
}
process.exit(failed ? 1 : 0);

function versionOfRepo() {
  const m = /__version__\s*=\s*"(\d+\.\d+\.\d+)"/.exec(fs.readFileSync(path.join(repo, "backend/hokma/__init__.py"), "utf8"));
  return m[1];
}
