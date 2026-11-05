/**
 * Shared helpers of the E2E suites: the real backend on a free port, the real compiled dashboard, a real Chromium.
 * No UI text lives in the suites: every label comes from the message files (src/i18n/messages) in the tested language.
 */
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

export const here = path.dirname(fileURLToPath(import.meta.url));
export const dashboard = path.resolve(here, "..");
export const repo = path.resolve(dashboard, "..");

export function loadPlaywright() {
  return createRequire(path.join(dashboard, "noop.js"))("playwright");
}

/** Every key of the language, from all the message files. */
export function loadMessages(locale) {
  const out = {};
  const dir = path.join(dashboard, "src/i18n/messages");
  for (const file of fs.readdirSync(dir).filter((f) => f.endsWith(".ts"))) {
    const source = fs.readFileSync(path.join(dir, file), "utf8");
    const es = source.indexOf("  es: {");
    const en = source.indexOf("  en: {");
    const block = locale === "es" ? source.slice(es, en > es ? en : undefined) : source.slice(en);
    for (const m of block.matchAll(/^\s+"([\w.]+)":\s*("(?:[^"\\]|\\.)*"),?$/gm)) out[m[1]] = JSON.parse(m[2]);
  }
  return out;
}

export function translator(locale) {
  const M = loadMessages(locale);
  const msg = (key, params = {}) => {
    if (!(key in M)) throw new Error(`missing message key ${key}`);
    return M[key].replace(/\{(\w+)\}/g, (_, k) => String(params[k]));
  };
  return { M, msg };
}

export const free = () =>
  new Promise((resolve) => {
    const s = net.createServer().listen(0, "127.0.0.1", () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function until(fn, what, ms = 15000) {
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

/** Builds the dashboard once per process into a temporary folder (or reuses E2E_DIST when the runner built it). */
export function buildDashboard() {
  if (process.env.E2E_DIST) return process.env.E2E_DIST;
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), "hokma-e2e-dist-"));
  const r = spawnSync("npx", ["vite", "build", "--outDir", outDir, "--emptyOutDir"], { cwd: dashboard, encoding: "utf8" });
  if (r.status !== 0) throw new Error(`vite build failed:\n${r.stdout}\n${r.stderr}`);
  return outDir;
}

/**
 * Starts `script` (a python file of e2e/) with temporary HOKMA_HOME and HOKMA_DATA_DIR and a fake `claude` first
 * on PATH. With `signedIn` the fake answers `claude auth status` as a connected Pro plan; everything else is the
 * recorded stream-json fake of the engine tests.
 */
export async function startServer({ script, root, dist, signedIn = false }) {
  const home = path.join(root, "home");
  const data = path.join(root, "data");
  const bin = path.join(root, "bin");
  for (const d of [home, data, bin]) fs.mkdirSync(d, { recursive: true });
  const fakeLog = path.join(root, "fake.log");
  const fake = path.join(repo, "backend/tests/engine_fixtures/fake_claude.py");
  const auth = signedIn
    ? `if sys.argv[1:2] == ["auth"]:\n    print(json.dumps({"loggedIn": True, "subscriptionType": "pro"}))\n    sys.exit(0)\nif sys.argv[1:2] == ["--version"]:\n    print("2.0.0 (Claude Code)")\n    sys.exit(0)\n`
    : "";
  fs.writeFileSync(
    path.join(bin, "claude"),
    `#!/usr/bin/env python3\nimport json, runpy, sys\n${auth}runpy.run_path(${JSON.stringify(fake)}, run_name="__main__")\n`,
  );
  fs.chmodSync(path.join(bin, "claude"), 0o755);
  const port = await free();
  const child = spawn("uv", ["run", "--project", repo, "python", path.join(here, script), dist, String(port)], {
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
  const base = `http://127.0.0.1:${port}`;
  const api = async (p, init = {}) => {
    const res = await fetch(`${base}${p}`, { ...init, headers: { "X-Hokma-Token": info.token, "Content-Type": "application/json", ...(init.headers ?? {}) } });
    return res.json();
  };
  return {
    child,
    port,
    base: `${base}/`,
    token: info.token,
    home,
    data,
    fakeLog,
    api,
    async stop() {
      child.kill("SIGTERM");
      await sleep(300);
      if (child.exitCode === null) child.kill("SIGKILL");
    },
  };
}

/**
 * Runs one scenario per locale (or the one named in argv[2]) with a clean server and a fresh browser each time,
 * prints `ok <locale>` or `FAIL <locale>` and exits non-zero when any fails.
 */
export async function runSuite(name, { script, signedIn = false }, scenario) {
  const locales = process.argv[2] ? [process.argv[2]] : ["es", "en"];
  const { chromium } = loadPlaywright();
  const dist = buildDashboard();
  let failed = false;
  for (const locale of locales) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), `hokma-${name}-e2e-${locale}-`));
    const server = await startServer({ script, root, dist, signedIn });
    const browser = await chromium.launch({ headless: true, ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}) });
    try {
      const ctx = await browser.newContext({ viewport: { width: 1440, height: 940 } });
      await ctx.addInitScript(([token, loc]) => {
        if (!localStorage.getItem("hokma.token")) localStorage.setItem("hokma.token", token);
        if (!localStorage.getItem("hokma.locale")) localStorage.setItem("hokma.locale", loc);
      }, [server.token, locale]);
      const page = await ctx.newPage();
      const errors = [];
      page.on("pageerror", (e) => errors.push(e.message));
      await scenario({ locale, page, server, root, errors, ...translator(locale) });
      if (errors.length) throw new Error(`uncaught page errors: ${errors.join(" | ")}`);
      console.log(`ok ${name} ${locale}`);
    } catch (error) {
      failed = true;
      console.error(`FAIL ${name} ${locale}:`, error.stack || error);
      if (process.env.E2E_DEBUG_DIR) {
        const page = browser.contexts()[0]?.pages()[0];
        if (page) {
          await page.screenshot({ path: path.join(process.env.E2E_DEBUG_DIR, `fail-${name}-${locale}.png`) }).catch(() => undefined);
          fs.writeFileSync(path.join(process.env.E2E_DEBUG_DIR, `fail-${name}-${locale}.txt`), await page.locator("body").innerText().catch(() => ""));
        }
      }
    } finally {
      await browser.close();
      await server.stop();
    }
  }
  process.exit(failed ? 1 : 0);
}
