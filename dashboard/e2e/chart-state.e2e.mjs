/**
 * Chart state E2E (SPEC-001 addendum, chart state): what each chart tab remembers across a reload, a closed browser and
 * an engine reconnection, against the real engine, the real compiled dashboard and a real Chromium with a persistent
 * profile. The market is a fake provider (e2e/chart-state-serve.py) that streams 4 messages per second and counts what
 * the engine asks of it. Tests R-01 to R-12 and L-01 to L-05 of the addendum.
 *
 *   node e2e/chart-state.e2e.mjs [es|en]        (both locales when omitted)
 *   E2E_ONLY=R-01,L-03 node e2e/chart-state.e2e.mjs es
 *
 * The addendum names NQ=F, ES=F and BTC-USD as examples; the fake provider serves crypto pairs only, so the tests use
 * BTC-USD, ETH-USD and SOL-USD. Nothing in the behavior depends on the asset class.
 * L-05 (the regression suites) is not run here: it is `npm test`, `npm run typecheck` and `e2e/chart.e2e.mjs`.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { buildDashboard, handoffScript, loadPlaywright, sleep, startServer, translator, until } from "./lib.mjs";

const ONLY = process.env.E2E_ONLY ? new Set(process.env.E2E_ONLY.split(",")) : null;
const STORE = "hokma.chart.tabs.v1";
const AXIS_WIDTH = 78;
const TIME_AXIS_HEIGHT = 30;
const SAVE_WAIT = 450; // the view is written 300 ms after the last movement

const launchOptions = (extra = {}) => ({
  headless: true,
  viewport: { width: 1440, height: 940 },
  ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
  ...extra,
});

/** One browser on one persistent profile, and everything it said on the market socket. */
async function openBrowser(chromium, profile, server, locale) {
  const ctx = await chromium.launchPersistentContext(profile, launchOptions());
  await ctx.addInitScript({ content: handoffScript(server.token) });
  await ctx.addInitScript((loc) => {
    if (!localStorage.getItem("hokma.locale")) localStorage.setItem("hokma.locale", loc);
  }, locale);
  const page = ctx.pages()[0] ?? (await ctx.newPage());
  const seen = { out: [], inbound: [], errors: [], pageErrors: [] };
  page.on("websocket", (ws) => {
    if (!ws.url().includes("/ws/market")) return;
    ws.on("framesent", (frame) => {
      try {
        seen.out.push({ ...JSON.parse(String(frame.payload)), at: Date.now() });
      } catch {
        /* not JSON */
      }
    });
    ws.on("framereceived", (frame) => {
      try {
        seen.inbound.push({ ...JSON.parse(String(frame.payload)), at: Date.now() });
      } catch {
        /* not JSON */
      }
    });
  });
  page.on("pageerror", (e) => seen.pageErrors.push(e.message));
  page.on("console", (m) => {
    // The owner confirmation of the tab is refused (403) in this harness on every load: not the chart's business.
    if (m.type() === "error" && !m.location().url.endsWith("/api/session/owner-nonce")) seen.errors.push(m.text());
  });
  return { ctx, page, seen };
}

// ---- reading the page ------------------------------------------------------------------------------------------------

const front = (page) => page.locator(".screen-host:not([hidden])");
const body = (page) => front(page).getByTestId("chart-body");
const parse = (raw) => (raw ? JSON.parse(raw) : null);
const view = async (page) => parse(await body(page).getAttribute("data-view"));
const attr = async (page, name) => Number((await body(page).getAttribute(name)) ?? NaN);
const titleOf = async (page) => (await front(page).getByTestId("chart-title").innerText()).replace(/\s+/g, " ").trim();
const stored = (page) => page.evaluate((key) => JSON.parse(localStorage.getItem(key) ?? "null"), STORE);
const openTabKeys = (page) => page.evaluate(() => (JSON.parse(localStorage.getItem("hokma.ui.v1") ?? "{}").tabs ?? []).map((t) => t.key));

/**
 * The chart in front has bars and a view, and says its title. The view is read once it has settled: the chart library
 * reports the range of a new data set on its next frame, and a read in between would give its default zoom and not the
 * view that is about to be placed.
 */
async function ready(page, title) {
  await body(page).waitFor();
  await until(async () => (await attr(page, "data-bars")) > 20 && (await view(page)) !== null, "chart with bars and a view");
  const shape = async () => {
    const v = await view(page);
    return v ? `${v.follow}|${v.barSpacing}|${v.rightOffset}` : null;
  };
  let last = await shape();
  await until(async () => {
    await sleep(350);
    const now = await shape();
    const settled = now !== null && now === last;
    last = now;
    return settled;
  }, "the view settled");
  if (title) await until(async () => (await titleOf(page)) === title, `title ${title}`);
}

async function geometry(page) {
  const box = await front(page).getByTestId("price-chart").boundingBox();
  return { cx: box.x + (box.width - AXIS_WIDTH) / 2, cy: box.y + (box.height - TIME_AXIS_HEIGHT) / 2, axisX: box.x + box.width - AXIS_WIDTH / 2, right: box.x + box.width - AXIS_WIDTH - 4, left: box.x + 4 };
}

async function wheel(page, notches) {
  const g = await geometry(page);
  await page.mouse.move(g.cx, g.cy);
  for (let i = 0; i < Math.abs(notches); i += 1) {
    await page.mouse.wheel(0, notches < 0 ? -120 : 120);
    await sleep(40);
  }
}

/** Drags the bars to the right by `bars` bars (older bars come into view). Negative drags them to the left. */
async function dragBars(page, bars) {
  const v = await view(page);
  const g = await geometry(page);
  const px = Math.round(bars * v.barSpacing);
  await page.mouse.move(g.cx, g.cy);
  await page.mouse.down();
  await page.mouse.move(g.cx + px, g.cy, { steps: 30 });
  await page.mouse.up();
}

async function dragPriceScale(page, dy) {
  const g = await geometry(page);
  await page.mouse.move(g.axisX, g.cy);
  await page.mouse.down();
  await page.mouse.move(g.axisX, g.cy + dy, { steps: 20 });
  await page.mouse.up();
}

const settle = () => sleep(SAVE_WAIT);

// ---- driving the page ------------------------------------------------------------------------------------------------

async function freshStart(page, server) {
  await page.goto(server.base);
  await page.evaluate(() => {
    for (const key of ["hokma.ui.v1", "hokma.chart.tabs.v1", "hokma.chart.timeframe", "hokma.chart.recent", "hokma.chart.intervals"]) localStorage.removeItem(key);
  });
  await page.reload();
  await page.getByRole("button", { name: /./ }).first().waitFor();
}

async function openEmptyChart(page, msg) {
  await page.getByRole("button", { name: msg("nav.chart"), exact: true }).click();
  await page.getByRole("textbox", { name: msg("chart.search.label") }).waitFor();
}

async function pickOnEmpty(page, msg, symbol) {
  const search = page.getByRole("textbox", { name: msg("chart.search.label") });
  await search.fill(symbol);
  await search.press("Enter");
}

/** A new chart tab with the symbol, through the sidebar and the empty screen. */
async function openChart(page, msg, symbol) {
  await openEmptyChart(page, msg);
  await pickOnEmpty(page, msg, symbol);
  await ready(page);
  await until(async () => (await titleOf(page)).startsWith(symbol), `${symbol} in front`);
}

/** The interval through the keyboard, the way a student does it: a digit on the chart, the rest in the box. */
async function setInterval_(page, msg, text) {
  await body(page).focus();
  await page.keyboard.press(text[0]);
  const box = page.getByRole("combobox", { name: msg("chart.interval.label") });
  await box.waitFor();
  await page.keyboard.type(text.slice(1));
  await page.keyboard.press("Enter");
  await ready(page);
  await until(async () => (await titleOf(page)).endsWith(`· ${text}`), `interval ${text}`);
}

async function changeSymbol(page, msg, symbol) {
  await front(page).getByTestId("chart-symbol").click();
  const search = page.getByRole("combobox", { name: msg("chart.search.label") });
  await search.fill(symbol);
  await search.press("Enter");
  await ready(page);
  await until(async () => (await titleOf(page)).startsWith(symbol), `${symbol} in front`);
}

/**
 * Puts `text` in the chart store as the page goes away, after the page has written its own state on the way out (it
 * writes on pagehide and when it turns hidden), so the next load finds exactly that and no global interval to fall back on.
 */
const corruptOnLeave = (page, text) =>
  page.evaluate(
    ([key, value]) => {
      const corrupt = () => {
        localStorage.setItem(key, value);
        localStorage.removeItem("hokma.chart.timeframe");
      };
      window.addEventListener("pagehide", corrupt);
      document.addEventListener("visibilitychange", corrupt); // Chromium sends this one after pagehide on a reload
    },
    [STORE, text],
  );

const reload = async (page) => {
  await page.reload();
  await ready(page);
};

const closeTab = (page, msg, title) => page.getByRole("button", { name: msg("top.closeTab", { title }) }).click();

/** L-01: live price after a reload, in 5 s or less, and it changes with every message of the provider. */
async function livePrice(page) {
  const price = front(page).getByTestId("chart-price");
  await until(async () => (await price.getAttribute("data-live")) === "true", "data-live true", 5000);
  const first = await price.innerText();
  await until(async () => (await price.innerText()) !== first, "the price changes", 4000);
}

// ---- comparisons -----------------------------------------------------------------------------------------------------

/** Same bars on screen (margin of one bar) and the same spacing (margin 0.01). */
function sameView(before, after, barSeconds, what) {
  if (process.env.E2E_VERBOSE) console.log(`    ${what}: before ${JSON.stringify(before)} after ${JSON.stringify(after)}`);
  assert.ok(Math.abs(before.from - after.from) <= barSeconds, `${what}: first visible bar ${before.from} -> ${after.from}`);
  assert.ok(Math.abs(before.to - after.to) <= barSeconds, `${what}: last visible bar ${before.to} -> ${after.to}`);
  assert.ok(Math.abs(before.barSpacing - after.barSpacing) <= 0.01, `${what}: barSpacing ${before.barSpacing} -> ${after.barSpacing}`);
}

// ---- the tests -------------------------------------------------------------------------------------------------------

function tests({ chromium, server, profile, locale, msg, holder }) {
  const list = [];
  const test = (id, fn) => list.push({ id, fn });
  const b = new Proxy({}, { get: (_, key) => holder.b[key] }); // the current browser: R-04 closes and reopens it

  test("R-01", async () => {
    await freshStart(b.page, server);
    await openChart(b.page, msg, "BTC-USD");
    await setInterval_(b.page, msg, "7m");
    await wheel(b.page, -4);
    await dragBars(b.page, 100);
    await settle();
    const before = await view(b.page);
    assert.equal(before.follow, false, "scrolled back, not following");
    await reload(b.page);
    assert.equal(await titleOf(b.page), "BTC-USD · 7m");
    sameView(before, await view(b.page), 420, "R-01");
    await livePrice(b.page);
  });

  test("R-02", async () => {
    await freshStart(b.page, server);
    await openChart(b.page, msg, "BTC-USD");
    await setInterval_(b.page, msg, "7m");
    await wheel(b.page, -3);
    await dragBars(b.page, -400); // all the way to the right edge
    await settle();
    const before = await view(b.page);
    assert.equal(before.follow, true, "at the right edge");
    await reload(b.page);
    const lastBefore = await attr(b.page, "data-last-bar");
    const after = await view(b.page);
    assert.equal(after.follow, true);
    assert.ok(Math.abs(after.rightOffset - before.rightOffset) <= 0.6, `rightOffset ${before.rightOffset} -> ${after.rightOffset}`);
    assert.ok(Math.abs(after.barSpacing - before.barSpacing) <= 0.01, "same zoom");
    await until(async () => (await attr(b.page, "data-last-bar")) >= lastBefore + 2 * 420, "two new bars", 40000);
    await until(async () => (await view(b.page)).to >= after.to + 2 * 420, "the view follows the new bars", 5000);
    const later = await view(b.page);
    assert.equal(later.follow, true, "still following");
    assert.ok(Math.abs(later.barSpacing - before.barSpacing) <= 0.01, "the zoom did not change");
    assert.ok(Math.abs(later.rightOffset - before.rightOffset) <= 0.6, "the last bar stayed at the same place from the right edge");
    assert.ok(later.to >= after.to + 2 * 420, "the new bars entered by the right");
    await livePrice(b.page);
  });

  test("R-03", async () => {
    await freshStart(b.page, server);
    await openChart(b.page, msg, "BTC-USD");
    await dragPriceScale(b.page, -90);
    await settle();
    const before = await view(b.page);
    assert.equal(before.price.auto, false, "the scale is manual after the drag");
    await reload(b.page);
    const after = await view(b.page);
    assert.equal(after.price.auto, false);
    for (const side of ["min", "max"]) {
      const a = before.priceRange[side];
      const c = after.priceRange[side];
      assert.ok(Math.abs(a - c) <= Math.abs(a) * 0.001, `price ${side} ${a} -> ${c}`);
    }
    await livePrice(b.page);
    const g = await geometry(b.page);
    await b.page.mouse.dblclick(g.axisX, g.cy);
    await settle();
    assert.equal((await view(b.page)).price.auto, true, "double click on the scale makes it automatic");
    await reload(b.page);
    assert.equal((await view(b.page)).price.auto, true, "and it stays automatic");
    await livePrice(b.page);
  });

  test("R-04", async () => {
    await freshStart(b.page, server);
    await openChart(b.page, msg, "BTC-USD");
    await setInterval_(b.page, msg, "7m");
    await wheel(b.page, -3);
    await dragBars(b.page, 40);
    await settle();
    const btc = await view(b.page);
    await openChart(b.page, msg, "ETH-USD");
    await setInterval_(b.page, msg, "1h");
    await wheel(b.page, -5);
    await dragBars(b.page, 25);
    await settle();
    const eth = await view(b.page);
    assert.ok(Math.abs(btc.barSpacing - eth.barSpacing) > 0.5, "the two charts have different zoom");

    await b.ctx.close(); // the browser, all of it
    holder.b = await openBrowser(chromium, profile, server, locale);
    await b.page.goto(server.base);
    await ready(b.page);
    const tabs = b.page.locator('.screen-host[data-screen="chart"]');
    assert.equal(await tabs.count(), 2);
    const read = async (index) => {
      const host = tabs.nth(index);
      const key = await host.getByTestId("chart-title").innerText();
      return { title: key.replace(/\s+/g, " ").trim(), view: parse(await host.getByTestId("chart-body").getAttribute("data-view")) };
    };
    const first = await read(0);
    const second = await read(1);
    assert.equal(first.title, "BTC-USD · 7m");
    assert.equal(second.title, "ETH-USD · 1h");
    // the tab behind was mounted hidden (no layout to read): bring each to the front and read what the chart shows
    await b.page.locator(".tab .tl", { hasText: "BTC-USD" }).click();
    await ready(b.page, "BTC-USD · 7m");
    await sleep(300);
    sameView(btc, await view(b.page), 420, "R-04 BTC");
    await b.page.locator(".tab .tl", { hasText: "ETH-USD" }).click();
    await ready(b.page, "ETH-USD · 1h");
    await sleep(300);
    sameView(eth, await view(b.page), 3600, "R-04 ETH");
    await livePrice(b.page);
  });

  test("R-05", async () => {
    await freshStart(b.page, server);
    await openChart(b.page, msg, "BTC-USD");
    await setInterval_(b.page, msg, "7m");
    await wheel(b.page, -4);
    await dragBars(b.page, 50);
    await settle();
    await changeSymbol(b.page, msg, "ETH-USD");
    assert.equal(await titleOf(b.page), "ETH-USD · 7m");
    await settle();
    const keys = Object.keys((await stored(b.page)).tabs);
    assert.deepEqual(keys, ["chart:symbol=ETH-USD"], "the old key is gone");
    assert.equal((await stored(b.page)).tabs["chart:symbol=ETH-USD"].view, null, "the view went back to the default one");
    await reload(b.page);
    assert.equal(await titleOf(b.page), "ETH-USD · 7m");
    assert.deepEqual(Object.keys((await stored(b.page)).tabs), ["chart:symbol=ETH-USD"]);
    await livePrice(b.page);
  });

  test("R-06", async () => {
    await freshStart(b.page, server);
    await openChart(b.page, msg, "BTC-USD");
    await setInterval_(b.page, msg, "1h");
    await openChart(b.page, msg, "ETH-USD");
    await setInterval_(b.page, msg, "7m");
    await wheel(b.page, -4);
    await dragBars(b.page, 30);
    await settle();
    const eth = await view(b.page);
    await b.page.locator(".tab .tl", { hasText: "BTC-USD" }).click();
    await ready(b.page, "BTC-USD · 1h");
    await changeSymbol(b.page, msg, "ETH-USD"); // ETH-USD already has its tab: the two merge
    await ready(b.page, "ETH-USD · 7m");
    await sleep(300);
    assert.equal((await openTabKeys(b.page)).filter((k) => k.startsWith("chart:")).length, 1, "one chart tab left");
    sameView(eth, await view(b.page), 420, "R-06");
    await settle();
    assert.deepEqual(Object.keys((await stored(b.page)).tabs), ["chart:symbol=ETH-USD"], "no orphan entry");
  });

  test("R-07", async () => {
    await freshStart(b.page, server);
    await openChart(b.page, msg, "BTC-USD");
    await setInterval_(b.page, msg, "7m");
    await wheel(b.page, -4);
    await dragBars(b.page, 60);
    await settle();
    const before = await view(b.page);
    const snapshots = () => b.seen.inbound.filter((m) => m.type === "snapshot").length;
    const before_ = snapshots();
    await server.api("/api/e2e/drop", { method: "POST" }); // the provider falls and comes back
    await until(async () => snapshots() > before_, "the snapshot of the reconnection", 20000);
    await sleep(500);
    sameView(before, await view(b.page), 420, "R-07");
    await livePrice(b.page);
  });

  test("R-08", async () => {
    await freshStart(b.page, server);
    await openChart(b.page, msg, "BTC-USD");
    await openChart(b.page, msg, "ETH-USD");
    await setInterval_(b.page, msg, "7m");
    await settle();
    const tabsBefore = await openTabKeys(b.page);
    b.seen.errors.length = 0;
    b.seen.pageErrors.length = 0;
    await corruptOnLeave(b.page, "{broken");
    await reload(b.page);
    assert.equal((await titleOf(b.page)).endsWith("· 5m"), true, `default interval, got ${await titleOf(b.page)}`);
    assert.equal(await front(b.page).getByRole("alert").count(), 0, "nothing on screen");
    assert.deepEqual(b.seen.errors, [], "no console error");
    assert.deepEqual(b.seen.pageErrors, [], "no uncaught error");
    assert.deepEqual(await openTabKeys(b.page), tabsBefore, "the other tabs are intact");
    await corruptOnLeave(
      b.page,
      JSON.stringify({ v: 1, tabs: { "chart:symbol=ETH-USD": { tf: "99M", view: { follow: false, barSpacing: 9, rightOffset: 0, from: 1, to: 2, price: { auto: true } }, indicators: [], updated_at: 1 } }, last: null }),
    );
    await reload(b.page);
    assert.equal(await titleOf(b.page), "ETH-USD · 5m", "an interval the grammar rejects becomes the default");
    assert.deepEqual(b.seen.errors, [], "no console error");
  });

  test("R-09", async () => {
    await freshStart(b.page, server);
    await openChart(b.page, msg, "ETH-USD");
    await setInterval_(b.page, msg, "15m");
    await wheel(b.page, -4);
    await dragBars(b.page, 40);
    await settle();
    const before = await view(b.page);
    await closeTab(b.page, msg, "ETH-USD");
    await b.page.getByRole("button", { name: msg("nav.chart"), exact: true }).click(); // a chart without an asset
    await ready(b.page, "ETH-USD · 15m");
    sameView(before, await view(b.page), 900, "R-09");
  });

  test("R-10", async () => {
    await freshStart(b.page, server);
    await openChart(b.page, msg, "ETH-USD");
    await openEmptyChart(b.page, msg);
    await b.page.getByRole("textbox", { name: msg("chart.search.label") }).waitFor();
    const keys = await openTabKeys(b.page);
    assert.ok(keys.includes("chart") && keys.includes("chart:symbol=ETH-USD"), `two chart tabs, got ${keys}`);
    assert.equal(keys.filter((k) => k.startsWith("chart")).length, 2);
  });

  test("R-11", async () => {
    await freshStart(b.page, server);
    await openChart(b.page, msg, "BTC-USD");
    await wheel(b.page, -2);
    await settle();
    const first = await view(b.page);
    await wheel(b.page, -3); // the last zoom, and F5 before the 300 ms are over
    const last = await view(b.page);
    assert.ok(last.barSpacing > first.barSpacing + 0.5, "the last zoom changed the view");
    await b.page.reload();
    await ready(b.page);
    assert.ok(Math.abs((await view(b.page)).barSpacing - last.barSpacing) <= 0.01, "the view of the last zoom came back");
  });

  test("R-12", async () => {
    await freshStart(b.page, server);
    await openChart(b.page, msg, "BTC-USD");
    await wheel(b.page, -2);
    await settle();
    for (let i = 1; i <= 60; i += 1) {
      const symbol = `S${String(i).padStart(2, "0")}-USD`;
      await openChart(b.page, msg, symbol);
      await closeTab(b.page, msg, symbol);
      const state = await stored(b.page);
      assert.ok(Object.keys(state.tabs).length <= 50, `at most 50 entries, got ${Object.keys(state.tabs).length} at ${symbol}`);
    }
    const state = await stored(b.page);
    assert.ok(Object.keys(state.tabs).length <= 50);
    assert.ok(state.last && state.last.symbol, "last is still there");
  });

  test("L-01", async () => {
    // the live price after each reload is checked inside R-01 to R-05 (livePrice); this is the explicit run
    await freshStart(b.page, server);
    await openChart(b.page, msg, "BTC-USD");
    for (let i = 0; i < 3; i += 1) {
      await wheel(b.page, -1);
      await settle();
      await reload(b.page);
      await livePrice(b.page);
    }
  });

  test("L-02", async () => {
    await freshStart(b.page, server);
    await openChart(b.page, msg, "BTC-USD");
    await setInterval_(b.page, msg, "1m");
    await sleep(500);
    const setData0 = await attr(b.page, "data-set-data");
    const updates0 = await attr(b.page, "data-updates");
    const bars0 = b.seen.inbound.filter((m) => m.type === "bar").length;
    const mark = Date.now();
    let step = 0;
    while (Date.now() - mark < 60000) {
      await wheel(b.page, step % 2 ? 1 : -1);
      await dragBars(b.page, step % 2 ? -6 : 6);
      step += 1;
      await sleep(1500);
    }
    await settle();
    const setData1 = await attr(b.page, "data-set-data");
    const updates1 = await attr(b.page, "data-updates");
    const barMessages = b.seen.inbound.filter((m) => m.type === "bar" && m.at >= mark).length;
    const total = b.seen.inbound.filter((m) => m.at >= mark).length;
    assert.equal(setData1, setData0, "setData only on snapshots: none in these 60 s");
    assert.ok(updates1 - updates0 >= 60, `update on live changes: ${updates1 - updates0} calls`);
    // messages that reach the page in the same frame are drawn together, so never more updates than messages
    assert.ok(updates1 - updates0 <= barMessages + 3, `${updates1 - updates0} calls for ${barMessages} bar messages (${total} messages of any kind)`);
  });

  test("L-03", async () => {
    await freshStart(b.page, server);
    await openChart(b.page, msg, "BTC-USD");
    await setInterval_(b.page, msg, "7m");
    await settle();
    const frames = () => b.seen.out.filter((m) => m.op === "subscribe" || m.op === "unsubscribe");
    const counters = async () => server.api("/api/e2e/counters");
    const before = frames().length;
    for (let i = 0; i < 30; i += 1) {
      if (i % 3 === 0) await wheel(b.page, i % 2 ? 1 : -1);
      else if (i % 3 === 1) await dragBars(b.page, i % 2 ? 5 : -5);
      else await dragPriceScale(b.page, i % 2 ? 20 : -20);
    }
    await settle();
    assert.equal(frames().length, before, "30 movements: no subscribe or unsubscribe");

    // a reload today (nothing saved) as the reference for the provider
    const costOf = async (doReload) => {
      await sleep(800);
      const c0 = await counters();
      const f0 = frames().length;
      await doReload();
      await sleep(1500);
      const c1 = await counters();
      return { connect: c1.connect - c0.connect, subscribe: c1.subscribe - c0.subscribe, unsubscribe: c1.unsubscribe - c0.unsubscribe, panel: frames().slice(f0) };
    };
    const baseline = await costOf(async () => {
      await b.page.evaluate((key) => localStorage.removeItem(key), STORE);
      await reload(b.page);
    });
    assert.deepEqual(baseline.panel.map((m) => m.op), ["subscribe"], "a reload without saved state: one subscribe");
    for (let i = 0; i < 5; i += 1) {
      await wheel(b.page, -1);
      await dragBars(b.page, 10);
      await settle();
      const saved = await costOf(() => reload(b.page));
      assert.deepEqual(saved.panel.map((m) => m.op), ["subscribe"], `reload ${i + 1}: one subscription`);
      assert.equal(saved.panel[0].symbol, "BTC-USD");
      assert.equal(saved.panel[0].tf, "7m", "with the restored interval");
      assert.deepEqual({ connect: saved.connect, subscribe: saved.subscribe }, { connect: baseline.connect, subscribe: baseline.subscribe }, `reload ${i + 1}: the provider sees what it sees today`);
    }
  });

  test("L-04", async () => {
    await freshStart(b.page, server);
    await openChart(b.page, msg, "BTC-USD");
    await setInterval_(b.page, msg, "7m");
    await wheel(b.page, -3);
    await dragBars(b.page, 30);
    await settle();
    const before = await view(b.page);
    assert.equal(before.follow, false);
    const last0 = await attr(b.page, "data-last-bar");
    await until(async () => (await attr(b.page, "data-last-bar")) >= last0 + 2 * 420, "new bars while scrolled back", 40000);
    const during = await view(b.page);
    assert.equal(during.from, before.from, "the first visible bar did not move");
    assert.equal(during.to, before.to, "the last visible bar did not move");
    assert.ok(Math.abs(during.barSpacing - before.barSpacing) <= 0.01);
    // back to the right edge: following again, and saved
    await dragBars(b.page, -400);
    await settle();
    assert.equal((await view(b.page)).follow, true, "following again");
    const state = await stored(b.page);
    assert.equal(state.tabs["chart:symbol=BTC-USD"].view.follow, true, "and saved");
  });

  return list;
}

/** One test: its own engine, its own fake provider (clock, counters) and its own browser profile. */
async function runTest(chromium, locale, dist, id, msg) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), `hokma-chart-state-e2e-${locale}-${id}-`));
  const profile = path.join(root, "profile");
  const server = await startServer({ script: "chart-state-serve.py", root, dist });
  const holder = { b: await openBrowser(chromium, profile, server, locale) };
  try {
    const test = tests({ chromium, server, profile, locale, msg, holder }).find((t) => t.id === id);
    await test.fn();
    if (holder.b.seen.pageErrors.length) throw new Error(`uncaught page errors: ${holder.b.seen.pageErrors.join(" | ")}`);
  } catch (error) {
    if (process.env.E2E_DEBUG_DIR) await holder.b.page.screenshot({ path: path.join(process.env.E2E_DEBUG_DIR, `chart-state-${id}-${locale}.png`) }).catch(() => undefined);
    throw error;
  } finally {
    await holder.b.ctx.close().catch(() => undefined);
    await server.stop();
  }
}

async function scenario(chromium, locale, dist) {
  const { msg } = translator(locale);
  let failed = false;
  for (const { id } of tests({ holder: {} })) {
    if (ONLY && !ONLY.has(id)) continue;
    const started = Date.now();
    try {
      await runTest(chromium, locale, dist, id, msg);
      console.log(`  pass ${id} ${locale} (${Math.round((Date.now() - started) / 1000)}s)`);
    } catch (error) {
      failed = true;
      console.error(`  FAIL ${id} ${locale}:`, error.message);
    }
  }
  if (failed) throw new Error(`chart-state ${locale} failed`);
  console.log(`ok chart-state ${locale}`);
}

const locales = process.argv[2] ? [process.argv[2]] : ["es", "en"];
const { chromium } = loadPlaywright();
const dist = buildDashboard();
let failed = false;
for (const locale of locales) {
  try {
    await scenario(chromium, locale, dist);
  } catch (error) {
    failed = true;
    console.error(error.message);
  }
}
process.exit(failed ? 1 : 0);
