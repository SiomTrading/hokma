/**
 * Chart E2E (D9): the real backend (e2e/chart-serve.py), the real compiled dashboard, a real Chromium and the fake
 * `claude` of backend/tests/engine_fixtures on PATH. Market data is replayed from recorded Yahoo answers
 * (e2e/market-replay), so nothing here uses the network. Runs the whole flow in es and en.
 *
 *   node e2e/chart.e2e.mjs [es|en]        (both locales when omitted)
 *
 * Needs the `playwright` package (dev dependency of dashboard/). If it is not installed here, point
 * PLAYWRIGHT_DIR to a folder whose node_modules has it. CHROMIUM_PATH overrides the browser binary.
 * E2E_DEBUG_DIR keeps a screenshot and the page text when a run fails; E2E_SHOT_DIR keeps screenshots of a good run.
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
const replay = path.join(here, "market-replay");

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
  for (const file of ["chart.ts", "core.ts", "chat.ts"]) {
    const source = fs.readFileSync(path.join(dashboard, "src/i18n/messages", file), "utf8");
    const start = source.indexOf(`  ${locale}: {`);
    const other = locale === "es" ? "  en: {" : "  es: {";
    const otherAt = source.indexOf(other);
    const block = locale === "es" ? source.slice(start, otherAt > start ? otherAt : undefined) : source.slice(start);
    for (const m of block.matchAll(/^\s+"([\w.]+)":\s*("(?:[^"\\]|\\.)*"),?$/gm)) out[m[1]] = JSON.parse(m[2]);
  }
  const backend = JSON.parse(fs.readFileSync(path.join(repo, "backend/hokma/catalog", `${locale}.json`), "utf8"));
  return { ...out, ...backend };
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

function buildDashboard(outDir) {
  const r = spawnSync("npx", ["vite", "build", "--outDir", outDir, "--emptyOutDir"], { cwd: dashboard, encoding: "utf8" });
  if (r.status !== 0) throw new Error(`vite build failed:\n${r.stdout}\n${r.stderr}`);
}

async function startServer(root, dist, locale) {
  const home = path.join(root, "home");
  const data = path.join(root, "data");
  const bin = path.join(root, "bin");
  for (const d of [home, data, bin]) fs.mkdirSync(d, { recursive: true });
  const fakeLog = path.join(root, "fake.log");
  const claude = path.join(bin, "claude");
  fs.writeFileSync(
    claude,
    `#!/usr/bin/env python3\nimport runpy\nrunpy.run_path(${JSON.stringify(path.join(repo, "backend/tests/engine_fixtures/fake_claude.py"))}, run_name="__main__")\n`,
  );
  fs.chmodSync(claude, 0o755);
  const port = await free();
  const child = spawn("uv", ["run", "--project", repo, "python", path.join(here, "chart-serve.py"), dist, String(port)], {
    cwd: repo,
    env: {
      ...process.env,
      HOKMA_HOME: home,
      HOKMA_DATA_DIR: data,
      HOKMA_LANG: locale,
      HOKMA_MARKET_REPLAY_DIR: replay,
      FAKE_CLAUDE_LOG: fakeLog,
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
  return { child, port, token: info.token, home, fakeLog };
}

const fakeEntries = (log, event) =>
  fs.existsSync(log) ? fs.readFileSync(log, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)).filter((e) => !event || e.event === event) : [];

/** Opening high computed here, apart from the product code: the highest high of the 09:30 to 09:59 New York bars of the last session. */
function expectedOpeningHigh(file, kind) {
  const raw = JSON.parse(fs.readFileSync(path.join(replay, file), "utf8")).chart.result[0];
  const highs = raw.indicators.quote[0].high;
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const local = (t) => Object.fromEntries(parts.formatToParts(new Date(t * 1000)).map((p) => [p.type, p.value]));
  const days = new Map();
  raw.timestamp.forEach((t, i) => {
    if (highs[i] === null) return;
    const l = local(t);
    const minutes = Number(l.hour) * 60 + Number(l.minute);
    if (minutes < 570 || minutes >= 600) return; // 09:30 to 09:59
    const day = `${l.year}-${l.month}-${l.day}`;
    days.set(day, Math.max(days.get(day) ?? -Infinity, highs[i]));
  });
  assert.ok(days.size > 0, `${kind}: the recording has an opening window`);
  const last = [...days.keys()].sort().at(-1);
  return days.get(last);
}

async function scenario(chromium, locale, dist) {
  const M = loadMessages(locale);
  const msg = (key, params = {}) => M[key].replace(/\{(\w+)\}/g, (_, k) => params[k]);
  const root = fs.mkdtempSync(path.join(os.tmpdir(), `hokma-chart-e2e-${locale}-`));
  const server = await startServer(root, dist, locale);
  const launch = { headless: true, ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}) };
  const browser = await chromium.launch(launch);
  try {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 940 } });
    await ctx.addInitScript(([token, loc]) => {
      localStorage.setItem("hokma.token", token);
      localStorage.setItem("hokma.locale", loc);
    }, [server.token, locale]);
    const page = await ctx.newPage();
    const errors = [];
    const outside = [];
    page.on("pageerror", (e) => errors.push(e.message));
    page.on("request", (r) => {
      const url = new URL(r.url());
      if (!["127.0.0.1", "localhost"].includes(url.hostname) && !["data:", "blob:"].includes(url.protocol)) outside.push(r.url());
    });
    await page.goto(`http://127.0.0.1:${server.port}/`);
    const shot = async (name) => {
      if (process.env.E2E_SHOT_DIR) await page.screenshot({ path: path.join(process.env.E2E_SHOT_DIR, `chart-${name}-${locale}.png`) });
    };

    // chart tabs stay mounted (hidden) in the background: every locator looks only at the tab in front
    const front = page.locator(".screen-host:not([hidden])");
    const candles = () => front.locator("[data-candle]");
    const title = front.getByTestId("chart-title");
    const opening = front.getByTestId("opening-line");
    const search = () => page.getByRole("textbox", { name: msg("chart.search.label") });
    const openAddMenu = async () => {
      await page.getByRole("button", { name: msg("top.addTab") }).click();
      await search().waitFor();
    };
    const openingPrice = async () => Number(await opening.getAttribute("data-price"));
    const tab = (symbol) => page.locator(".tab .tl", { hasText: symbol });

    // 1. the "+" menu lists well-known tickers before typing anything
    await openAddMenu();
    await page.getByRole("menuitem", { name: /ES=F/ }).waitFor();
    await page.getByRole("menuitem", { name: /NQ=F/ }).waitFor();

    // 2. searching for NQ finds the continuous future and opens it in its own tab
    await search().fill("nq");
    const hit = page.getByRole("menuitem", { name: /NQ=F/ }).first();
    await hit.waitFor();
    await hit.click();
    await tab("NQ=F").waitFor();
    await title.waitFor();
    assert.equal((await title.innerText()).replace(/\s+/g, " "), "NQ=F · 5m");
    await until(async () => (await candles().count()) > 20, "candles drawn");
    await front.getByRole("img", { name: msg("chart.aria", { symbol: "NQ=F", tf: "5m" }) }).waitFor();

    // 3. the page says whether the market is open (the answer depends on the clock when the run happens) and names the source
    const state = await front.getByTestId("market-state").innerText();
    assert.ok(state.includes(msg("chart.market.closed")) || state.includes(msg("chart.market.open")), "market state shown");
    await front.getByText(msg("chart.source", { source: "Yahoo Finance" })).waitFor();

    // 4. the opening high line is calculated from the real bars and matches an independent calculation
    const nq5 = await openingPrice();
    assert.equal(nq5, expectedOpeningHigh("chart_NQ_F_5m.json", "NQ=F 5m"));
    await front.getByText(msg("chart.opening"), { exact: false }).first().waitFor();
    await shot("nq-5m");

    // 5. changing the timeframe redraws
    const bars5 = await candles().count();
    await page.getByRole("button", { name: "15m", exact: true }).click();
    await until(async () => (await title.innerText()).includes("15m"), "15m title");
    await until(async () => (await candles().count()) !== bars5, "candles redrawn for 15m");
    await until(async () => (await opening.count()) === 1, "opening line on 15m");
    assert.equal(await openingPrice(), nq5, "15m bars give the same opening high as the 5m bars");
    await page.getByRole("button", { name: "1h", exact: true }).click();
    await until(async () => (await title.innerText()).includes("1h"), "1h title");
    await until(async () => (await opening.count()) === 1, "opening line on 1h");
    assert.equal(await openingPrice(), nq5, "1h opening high comes from the 5 minute bars");
    await page.getByRole("button", { name: "1D", exact: true }).click();
    await until(async () => (await title.innerText()).includes("1D"), "1D title");
    await until(async () => (await candles().count()) > 10, "daily candles");
    assert.equal(await opening.count(), 0, "no opening line on the daily chart");
    await shot("nq-1d");
    await page.getByRole("button", { name: "5m", exact: true }).click();
    await until(async () => (await opening.count()) === 1, "opening line back on 5m");

    // 6. a stock: its own tab, its own opening high
    await openAddMenu();
    await search().fill("aapl");
    await page.getByRole("menuitem", { name: /AAPL/ }).first().click();
    await tab("AAPL").waitFor();
    await until(async () => (await title.innerText()).startsWith("AAPL"), "AAPL chart");
    await until(async () => (await opening.count()) === 1, "AAPL opening line");
    assert.equal(await openingPrice(), expectedOpeningHigh("chart_AAPL_5m.json", "AAPL 5m"));
    // the recorded stock session is over whenever this runs: closed market, with the time of the last data
    const closed = await front.getByTestId("market-state").innerText();
    assert.ok(closed.includes(msg("chart.market.closed")), "stock market closed");
    assert.ok(/\d\d:\d\d/.test(closed), "last data time shown");
    assert.equal(await front.getByTestId("delay-notice").count(), 0, "a closed market is not a delayed one");
    await shot("aapl-closed");

    // 7. an unknown symbol is an error with a message, not an empty chart; typing a ticker and Enter opens it
    await openAddMenu();
    await search().fill("zzzz");
    await search().press("Enter");
    await tab("ZZZZ").waitFor();
    const alert = page.getByRole("alert").filter({ hasText: msg("market.error.unknown_symbol") });
    await alert.waitFor();
    assert.equal(await page.getByRole("button", { name: msg("chart.ask") }).isDisabled(), true);

    // 8. a network failure while loading shows the error state and the retry brings the chart back
    await tab("NQ=F").click();
    await title.waitFor();
    await page.route("**/api/market/candles**", (route) => route.abort());
    await page.getByRole("button", { name: "15m", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: msg("chart.error.title") }).waitFor();
    await page.unroute("**/api/market/candles**");
    await page.getByRole("button", { name: msg("ui.error.retry") }).click();
    await until(async () => (await candles().count()) > 5, "chart back after retry");
    await page.getByRole("button", { name: "5m", exact: true }).click();
    await until(async () => (await opening.count()) === 1, "NQ=F 5m again");
    await shot("error-recovered");

    // 9. ask the fund: the chart image goes to a new conversation and the chat tab opens
    await page.getByRole("button", { name: msg("chart.ask") }).click();
    await page.getByTestId("user-message").first().waitFor({ timeout: 20000 });
    await until(async () => (await page.getByTestId("assistant-message").last().innerText()).includes("pong"), "assistant answer", 20000);
    const sent = await until(() => fakeEntries(server.fakeLog, "user").at(-1), "the message reached the model");
    const blocks = sent.message.content;
    const image = blocks.find((b) => b.type === "image");
    assert.ok(image, "the model received an image block");
    assert.equal(image.source.media_type, "image/png");
    const png = Buffer.from(image.source.data, "base64");
    assert.equal(png.subarray(1, 4).toString("latin1"), "PNG");
    assert.equal(png.readUInt32BE(16), 1280, "image width");
    assert.equal(png.readUInt32BE(20), 720, "image height");
    const text = blocks.filter((b) => b.type === "text").map((b) => b.text).join(" ");
    assert.ok(text.includes("NQ=F") && text.includes("5m"), "the question names the symbol and the timeframe");
    const shown = new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(nq5);
    assert.ok(text.includes(shown), "the question carries the opening high");
    await shot("asked");
    assert.ok((await page.locator("aside .conv").first().innerText()).length > 0, "the conversation is listed in the left menu");

    assert.deepEqual(errors, [], "no uncaught page errors");
    assert.deepEqual(outside, [], "the panel only talks to 127.0.0.1");
    console.log(`ok ${locale}`);
  } catch (error) {
    if (process.env.E2E_DEBUG_DIR) {
      const page = browser.contexts()[0]?.pages()[0];
      if (page) {
        await page.screenshot({ path: path.join(process.env.E2E_DEBUG_DIR, `chart-fail-${locale}.png`) }).catch(() => undefined);
        fs.writeFileSync(path.join(process.env.E2E_DEBUG_DIR, `chart-fail-${locale}.txt`), await page.locator("body").innerText().catch(() => ""));
      }
      if (fs.existsSync(server.fakeLog)) fs.copyFileSync(server.fakeLog, path.join(process.env.E2E_DEBUG_DIR, `chart-fake-${locale}.log`));
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
const dist = fs.mkdtempSync(path.join(os.tmpdir(), "hokma-chart-e2e-dist-"));
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
