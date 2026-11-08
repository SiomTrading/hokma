/**
 * Connections and live price E2E: the real backend with the real hub (e2e/connections-serve.py), fake Coinbase,
 * Alpaca and Finnhub servers, an in-memory credential store, the real compiled dashboard and a real Chromium.
 *
 *   node e2e/connections.e2e.mjs [es|en]        (both locales when omitted)
 *   E2E_SHOT_DIR=<folder> keeps 1920x1080 screenshots of a good run
 *
 * C1  six cards, no key field on Coinbase, state and kind of data of every card equal to what the API says
 * C3  a saved key never comes back: not in the DOM, not in the list, not in any file under the data folder
 * C5  Settings has no key field and no provider selector
 * C6  "Connect a live source" opens Connections with the group of that class in front (and so does the address)
 * C7  a provider that goes down shows Reconnecting and returns to Connected without a reload
 * M1  the price on screen follows a trade in 250 ms or less (p95) and there are no /api/market requests after the snapshot
 * M10 crypto is live by Coinbase, a stock is delayed with the link until Alpaca has a key and then live, NQ is delayed with the link
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { runSuite, translator, until } from "./lib.mjs";

const digits = (text) => text.replace(/\D/g, "");

function filesUnder(folder) {
  const out = [];
  for (const entry of fs.existsSync(folder) ? fs.readdirSync(folder, { withFileTypes: true }) : []) {
    const full = path.join(folder, entry.name);
    if (entry.isDirectory()) out.push(...filesUnder(full));
    else out.push(full);
  }
  return out;
}

await runSuite("connections", { script: "connections-serve.py" }, async ({ page, server, msg, M, locale }) => {
  const control = async (p) => (await fetch(`http://127.0.0.1:${server.info.control}${p}`)).json();
  const { alpaca_key_id: KEY_ID, alpaca_secret: SECRET } = server.info;
  const requests = [];
  page.on("request", (r) => requests.push({ url: new URL(r.url()), at: Date.now() }));
  await page.setViewportSize({ width: 1920, height: 1080 });
  const shot = async (name) => {
    if (process.env.E2E_SHOT_DIR) await page.screenshot({ path: path.join(process.env.E2E_SHOT_DIR, `${name}-${locale}.png`) });
  };

  const front = page.locator(".screen-host:not([hidden])");
  const nav = () => page.locator("aside.side").getByRole("button", { name: msg("nav.connections"), exact: true });
  const card = (id) => front.getByTestId(`connection-${id}`);
  const search = () => page.getByRole("textbox", { name: msg("chart.search.label") });
  const status = () => front.getByTestId("live-status");
  const price = () => front.getByTestId("chart-price");
  const openChart = async (typed, symbol) => {
    await page.getByRole("button", { name: msg("top.addTab") }).click();
    await search().fill(typed);
    await search().press("Enter");
    await front.getByTestId("chart-title").filter({ hasText: symbol }).waitFor();
  };
  const closeChart = async (symbol) => {
    await page.getByRole("button", { name: msg("top.closeTab", { title: symbol }) }).click();
  };
  const mode = async (expected, what) => until(async () => (await status().getAttribute("data-mode")) === expected, `${what}: ${expected}`);

  await page.goto(server.base);
  await page.evaluate(() => {
    window.__stillHere = true;
  });

  // C1. Connections is its own section of the navigation, with the six cards visible at once
  await nav().click();
  await card("coinbase").waitFor();
  assert.equal(await front.locator("article[data-provider]").count(), 6);
  for (const name of ["Coinbase", "Alpaca", "Finnhub", "SiomTrading Futures", "Databento", "Massive"]) {
    assert.ok(await front.getByRole("article", { name }).isVisible(), `${name} visible`);
  }
  assert.equal(await card("coinbase").locator("input").count(), 0, "Coinbase has no key field");
  assert.equal(await card("coinbase").getByRole("switch").getAttribute("aria-checked"), "true", "Coinbase is on by default");
  assert.equal(await card("alpaca").locator('input[type="password"]').count(), 2, "Alpaca has Key ID and Secret");
  for (const id of ["finnhub", "siomtrading_futures", "databento", "massive"]) assert.equal(await card(id).locator("input").count(), 1, id);
  await card("alpaca").getByText(msg("connections.note.alpaca")).waitFor();
  await card("databento").getByText(msg("connections.note.premium")).waitFor();
  await until(async () => (await server.api("/api/connections")).connections.find((c) => c.id === "coinbase").state === "connected", "Coinbase connected in the engine");
  await until(async () => (await card("coinbase").getByTestId("conn-state").getAttribute("data-state")) === "connected", "Coinbase connected on its card");
  const list = await server.api("/api/connections");
  for (const c of list.connections) {
    assert.equal(await card(c.id).getByTestId("conn-state").getAttribute("data-state"), c.state, `${c.id} state`);
    assert.equal(await card(c.id).getByTestId("conn-data").getAttribute("data-data-state"), c.data_state, `${c.id} data state`);
    const symbols = msg("connections.symbols", { used: c.symbols_in_use, max: c.max_symbols });
    assert.equal((await card(c.id).getByTestId("conn-symbols").innerText()).trim(), symbols, `${c.id} symbols`);
  }
  assert.equal(await card("finnhub").getByTestId("conn-data").getAttribute("data-data-state"), "live", "Finnhub is live: its free trade stream is real time");
  assert.equal(await card("alpaca").getByTestId("conn-data").getAttribute("data-data-state"), "live");
  const futures = list.connections.find((c) => c.id === "siomtrading_futures");
  assert.equal(await card("siomtrading_futures").getByTestId("conn-unavailable").count(), futures.available ? 0 : 1, "SiomTrading Futures is shown, with the note while it is not ready");
  await shot("connections-six-cards");

  // M10 crypto: live through the fake Coinbase
  await openChart("btc-usd", "BTC-USD");
  await mode("live", "BTC-USD");
  assert.equal(await status().getAttribute("data-data-state"), "live");
  assert.equal(await price().getAttribute("data-live"), "true");
  await front.getByText(msg("chart.source", { source: "Coinbase" })).waitFor();
  assert.equal(await front.getByTestId("connect-link").count(), 0, "a live chart has no link to connect a source");
  await front.locator('[data-testid="chart-body"]:not([data-bars="0"])').first().waitFor();

  // M1. after the snapshot nothing asks /api/market/*, and the price follows the trades
  const snapshotAt = Date.now();
  await page.evaluate(() => {
    window.__prices = [];
    const target = document.querySelector('.screen-host:not([hidden]) [data-testid="chart-price"]');
    new MutationObserver(() => window.__prices.push({ at: Date.now(), text: target.textContent })).observe(target, { childList: true, characterData: true, subtree: true });
  });
  const delays = [];
  for (let i = 0; i < 14; i += 1) {
    const value = 81000 + i * 7 + 0.25;
    await control(`/tick?symbol=BTC-USD&price=${value}`);
    const sent = Date.now();
    const seen = await until(
      async () => page.evaluate(([want, since]) => window.__prices.find((p) => p.at >= since && p.text.replace(/\D/g, "") === want)?.at ?? null, [digits(value.toFixed(2)), sent - 5]),
      `price ${value} on screen`,
      4000,
    );
    delays.push(seen - sent);
    await sleep(380);
  }
  delays.sort((a, b) => a - b);
  const p95 = delays[Math.ceil(delays.length * 0.95) - 1];
  console.log(`  M1 trade to screen (${locale}): p95 ${p95} ms, max ${delays.at(-1)} ms, n ${delays.length}`);
  assert.ok(p95 <= 250, `the price follows a trade within 250 ms at p95 (got ${p95} ms)`);
  const polled = requests.filter((r) => r.at > snapshotAt && r.url.pathname.startsWith("/api/market/"));
  assert.deepEqual(polled.map((r) => r.url.pathname), [], "no /api/market request after the snapshot");
  await shot("chart-live");

  // M10 stock without a key: delayed, labelled, with the link; never painted as live
  await openChart("aapl", "AAPL");
  await until(async () => (await status().getAttribute("data-data-state")) === "delayed", "AAPL delayed");
  assert.equal(await status().getAttribute("data-data-state"), "delayed");
  assert.notEqual(await status().getAttribute("data-mode"), "live", "delayed data is never shown as live");
  assert.equal(await price().getAttribute("data-live"), "false");
  const link = front.getByTestId("connect-link");
  await link.waitFor();
  assert.equal((await link.innerText()).trim(), msg("chart.live.connect"));
  assert.equal(await link.getAttribute("href"), "#/connections?class=equity");
  // the recorded stock session is over whenever this runs: the bar may say "market closed", and then the delay is its own label
  const label = (await status().getAttribute("data-mode")) === "delayed" ? await front.getByTestId("market-state").innerText() : await front.getByTestId("delay-notice").innerText();
  assert.ok(label.includes(msg("chart.live.delayedMinutes", { minutes: 15 })), `the label says how late: ${label}`);
  await shot("chart-delayed-with-link");

  // C6. the link opens Connections with the group of the class in front
  await link.click();
  await card("alpaca").waitFor();
  const focused = async () => (await front.locator("[data-focus='true']").evaluateAll((nodes) => nodes.map((n) => n.getAttribute("data-testid")))).sort();
  assert.deepEqual(await focused(), ["connection-alpaca", "connection-databento", "connection-finnhub", "priority-equity"]);
  assert.equal(await page.evaluate(() => window.location.hash), "", "the address is clean");
  // the same by address
  await page.evaluate(() => {
    window.location.hash = "#/connections?class=future";
  });
  await until(async () => (await focused()).includes("priority-future"), "futures group in front by address");
  assert.deepEqual(await focused(), ["connection-databento", "connection-massive", "connection-siomtrading_futures", "priority-future"]);

  // M10 NQ: no SiomTrading Futures, no premium: delayed with the link to the futures group
  await openChart("nq=f", "NQ=F");
  await front.getByTestId("live-status").waitFor();
  await until(async () => (await status().getAttribute("data-data-state")) === "delayed", "NQ=F delayed");
  assert.equal(await front.getByTestId("connect-link").getAttribute("href"), "#/connections?class=future");

  // C3. a saved key never comes back
  await nav().click();
  await card("alpaca").waitFor();
  await card("alpaca").getByLabel(msg("connections.field.keyId")).fill(KEY_ID);
  await card("alpaca").getByLabel(msg("connections.field.secret")).fill(SECRET);
  await card("alpaca").getByRole("button", { name: msg("connections.key.save") }).click();
  await until(async () => (await card("alpaca").getByTestId("conn-key-state").getAttribute("data-key-state")) === "saved", "Alpaca key saved");
  assert.ok((await card("alpaca").getByTestId("conn-key-state").innerText()).includes(msg("connections.key.saved")));
  assert.ok(!(await page.content()).includes(SECRET), "the secret is not in the page");
  assert.equal(await page.locator("input").evaluateAll((nodes) => nodes.filter((n) => n.value !== "").length), 0, "no field keeps a value");
  assert.ok(!JSON.stringify(await server.api("/api/connections")).includes(SECRET), "the list does not carry it");
  for (const file of [...filesUnder(server.data), ...filesUnder(server.home)]) {
    assert.ok(!fs.readFileSync(file).includes(SECRET), `the secret is not in ${file}`);
  }
  // Test connection: Connected, kind of data and latency
  await card("alpaca").getByRole("button", { name: msg("connections.test") }).click();
  const result = card("alpaca").getByTestId("conn-test-result");
  await result.waitFor();
  assert.equal(await result.getAttribute("data-ok"), "true");
  assert.match(await result.innerText(), new RegExp(`${msg("connections.data.live")} · \\d+ ms`));
  await until(async () => (await card("alpaca").getByTestId("conn-state").getAttribute("data-state")) === "connected", "Alpaca connected");

  // M10 the same stock, now with a key: a new subscription goes through Alpaca and is live
  await closeChart("AAPL");
  await openChart("aapl", "AAPL");
  await mode("live", "AAPL with a key");
  await front.getByText(msg("chart.source", { source: "Alpaca IEX" })).waitFor();
  assert.equal(await front.getByTestId("connect-link").count(), 0);
  await control("/atick?symbol=AAPL&price=351.25");
  await until(async () => digits(await price().innerText()) === "35125", "Alpaca trade on screen");

  // "Remove key" takes it away
  await nav().click();
  await card("alpaca").getByRole("button", { name: msg("connections.key.remove") }).click();
  await until(async () => (await card("alpaca").getByTestId("conn-key-state").getAttribute("data-key-state")) === "missing", "Alpaca key removed");
  assert.equal((await server.api("/api/connections")).connections.find((c) => c.id === "alpaca").key_state, "missing");

  // C7. a provider goes down and comes back; the card follows without a reload
  const pill = () => card("coinbase").getByTestId("conn-state");
  await control("/drop?provider=coinbase");
  await until(async () => ["reconnecting", "error"].includes(await pill().getAttribute("data-state")), "Coinbase card shows the drop", 15000);
  assert.equal(await pill().getAttribute("data-state"), "reconnecting");
  assert.ok((await pill().innerText()).includes(msg("connections.state.reconnecting")));
  await shot("connections-reconnecting");
  await control("/up?provider=coinbase");
  await until(async () => (await pill().getAttribute("data-state")) === "connected", "Coinbase connected again", 30000);
  assert.ok((await pill().innerText()).includes(msg("connections.state.connected")));
  assert.equal(await page.evaluate(() => window.__stillHere === true), true, "the page was not reloaded");

  // C5. Settings has no key field and no provider selector, in either language
  const forbidden = new Set(["Alpaca", "Finnhub", "Coinbase", "Databento", "Massive"]);
  for (const other of ["en", "es"]) {
    const T = translator(other).M;
    for (const key of ["connections.field.key", "connections.field.token", "connections.field.keyId", "connections.field.secret", "connections.key.save", "connections.key.remove"]) forbidden.add(T[key]);
  }
  await page.getByRole("button", { name: M["nav.settings"] }).click();
  const dialog = page.getByRole("dialog", { name: msg("settings.title") });
  await dialog.waitFor();
  for (const section of ["general", "agents", "models", "extensions", "status", "news", "trash"]) {
    const button = dialog.getByRole("button", { name: msg(`settings.${section}`), exact: true });
    if ((await button.count()) === 0) continue;
    await button.click();
    await dialog.getByRole("heading", { name: msg(`settings.${section}`), exact: true }).waitFor();
    assert.equal(await dialog.locator('input[type="password"]').count(), 0, `${section}: no password field`);
    const text = await dialog.innerText();
    for (const word of forbidden) assert.ok(!text.includes(word), `${section}: "${word}" does not belong here`);
  }
  await page.getByRole("button", { name: msg("settings.close") }).click();
  assert.equal(await page.evaluate(() => window.__stillHere === true), true);
});

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
