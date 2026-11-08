/**
 * Shutdown E2E (task 3082, E-01 to E-04, E-06 and E-07): the Shut down Hokma button of the panel, its question, the
 * shutdown screen in every open tab, and the entry-screen form. Real backend (the lifecycle is wired like the real
 * engine), real compiled dashboard, real Chromium, es and en. E-05 and E-08 need the real `hokma start` and are
 * covered by backend/tests/test_shutdown_e2e.py and by the manual tests R-05.
 *
 *   node e2e/shutdown.e2e.mjs [es|en]
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { buildDashboard, handoffScript, loadPlaywright, sleep, startServer, translator, until } from "./lib.mjs";

const { chromium } = loadPlaywright();
const dist = buildDashboard();
const launchOptions = { headless: true, ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}) };

const alive = async (base) => {
  try {
    return (await fetch(`${base}health`, { signal: AbortSignal.timeout(900) })).ok;
  } catch {
    return false;
  }
};

/** A fresh engine and a fresh browser for one scenario. `session: false` opens the tab with no token (a new browser). */
async function scenario(locale, name, body, { session = true } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), `hokma-shutdown-e2e-${locale}-`));
  const server = await startServer({ script: "shutdown-serve.py", root, dist });
  const browser = await chromium.launch(launchOptions);
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 940 } });
    if (session) {
      await context.addInitScript({
        content: handoffScript(server.token, `if (${JSON.stringify(server.info.once)} && !window.location.hash) window.history.replaceState(null, "", "#o=" + ${JSON.stringify(server.info.once)});`),
      });
    }
    await context.addInitScript((loc) => {
      if (!localStorage.getItem("hokma.locale")) localStorage.setItem("hokma.locale", loc);
    }, locale);
    const errors = [];
    const open = async () => {
      const page = await context.newPage();
      page.on("pageerror", (e) => errors.push(e.message));
      await page.goto(server.base);
      return page;
    };
    await body({ ...translator(locale), server, context, open, locale });
    assert.deepEqual(errors, [], `uncaught page errors in ${name}`);
    console.log(`  ok ${name} ${locale}`);
  } finally {
    await browser.close();
    await server.stop();
  }
}

/** The entry-screen call as a program would make it: it asks for a nonce first, then sends it back. */
const post = async (server, headers = {}) => {
  const asked = await fetch(server.base + "api/system/shutdown-nonce", { headers: { "Sec-Fetch-Site": "same-origin" } });
  const { nonce } = await asked.json();
  return fetch(server.base + "api/system/shutdown", {
    method: "POST",
    headers: { Origin: server.base.replace(/\/$/, ""), "Sec-Fetch-Site": "same-origin", "X-Hokma-Shutdown-Nonce": nonce, ...headers },
  });
};

const locales = process.argv[2] ? [process.argv[2]] : ["es", "en"];
let failed = false;
for (const locale of locales) {
  try {
    // E-01 and E-02: the button is the last item under Settings; the question has its texts; Cancel and Escape call nothing
    await scenario(locale, "E-01/E-02", async ({ msg, server, open }) => {
      const page = await open();
      const menu = page.getByRole("complementary", { name: msg("nav.menu") });
      const button = menu.getByRole("button", { name: msg("nav.shutdown") });
      await button.waitFor();
      const names = await menu.getByRole("button").evaluateAll((els) => els.map((e) => e.getAttribute("aria-label") ?? e.textContent));
      assert.equal(names.at(-1), msg("nav.shutdown"));
      assert.equal(names.at(-2), msg("nav.settings"));

      await page.getByRole("button", { name: msg("top.sidebar") }).click();          // the menu cycles: next state is icon only
      await until(async () => (await page.getByTestId("shell").getAttribute("class")).split(" ").includes("sbc"), "collapsed menu");
      const collapsed = menu.getByRole("button", { name: msg("nav.shutdown") });
      assert.equal(await collapsed.getAttribute("title"), msg("nav.shutdown"));
      assert.equal(await collapsed.getAttribute("aria-label"), msg("nav.shutdown"));
      await collapsed.click();

      const calls = [];
      page.on("request", (r) => r.url().endsWith("/api/system/shutdown") && calls.push(r.method()));
      for (const dismiss of ["cancel", "escape"]) {
        if (dismiss === "escape") await menu.getByRole("button", { name: msg("nav.shutdown") }).click();
        const dialog = page.getByRole("alertdialog", { name: msg("shutdown.confirmTitle") });
        await dialog.waitFor();
        await dialog.getByText(msg("shutdown.confirmBody")).waitFor();
        await dialog.getByRole("button", { name: msg("shutdown.confirm") }).waitFor();
        if (dismiss === "cancel") await dialog.getByRole("button", { name: msg("shutdown.cancel") }).click();
        else await page.keyboard.press("Escape");
        await dialog.waitFor({ state: "detached" });
      }
      assert.deepEqual(calls, [], "cancelling calls nothing");
      assert.ok(await alive(server.base), "Hokma still answers /health");
    });

    // E-03: confirming shows the shutdown screen and the engine stops answering within 15 s
    await scenario(locale, "E-03", async ({ msg, server, open }) => {
      const page = await open();
      await page.getByRole("button", { name: msg("nav.shutdown") }).click();
      await page.getByRole("alertdialog").getByRole("button", { name: msg("shutdown.confirm") }).click();
      const screen = page.getByTestId("shutdown-screen");
      await screen.waitFor();
      await screen.getByRole("heading", { name: msg("shutdown.offTitle") }).waitFor();
      await screen.getByText(msg("shutdown.offBody")).waitFor();
      assert.equal(await screen.getByRole("button").count(), 0, "the shutdown screen has no buttons");
      await until(async () => !(await alive(server.base)), "/health to stop answering", 15000);
      await sleep(3000);                                             // and it stays like that: no reload, no reconnect screen
      assert.ok(await screen.isVisible());
    });

    // E-04: two tabs with a session; shutting down from one puts the other on the same screen within 3 s, for good
    await scenario(locale, "E-04", async ({ msg, server, open }) => {
      const one = await open();
      const two = await open();
      await one.getByRole("button", { name: msg("nav.shutdown") }).waitFor();
      await two.getByRole("button", { name: msg("nav.shutdown") }).waitFor();
      await sleep(500);                                              // both have opened their sockets
      await one.getByRole("button", { name: msg("nav.shutdown") }).click();
      await one.getByRole("alertdialog").getByRole("button", { name: msg("shutdown.confirm") }).click();
      const started = Date.now();
      await two.getByTestId("shutdown-screen").waitFor({ timeout: 3000 });
      assert.ok(Date.now() - started <= 3500, "the other tab got there in 3 s or less");
      await sleep(70_000);                                           // past the 60 s of the crash watch
      for (const page of [one, two]) {
        assert.ok(await page.getByTestId("shutdown-screen").isVisible());
        assert.equal(await page.getByText(msg("restart.failedTitle")).count(), 0, "never 'could not start'");
      }
    });

    // E-06: entry screen of a new browser, nobody else has Hokma open: the button shuts it down
    await scenario(locale, "E-06", async ({ msg, server, open }) => {
      const page = await open();
      await page.getByTestId("no-token").waitFor();
      await page.getByText(msg("noToken.body")).waitFor();
      await page.getByTestId("no-token-shutdown").click();
      await page.getByRole("alertdialog", { name: msg("shutdown.confirmTitle") }).getByRole("button", { name: msg("shutdown.confirm") }).click();
      await page.getByTestId("shutdown-screen").waitFor();
      await until(async () => !(await alive(server.base)), "/health to stop answering", 15000);
    }, { session: false });

    // E-07: entry screen while another tab holds a session: the in-use text, and Hokma stays up
    await scenario(locale, "E-07", async ({ msg, server, context, open }) => {
      // the tab with a session lives in a separate context with the handoff; the context of the scenario has none
      const browser = context.browser();
      const other = await browser.newContext({ viewport: { width: 1440, height: 940 } });
      await other.addInitScript({
        content: handoffScript(server.token, `if (${JSON.stringify(server.info.once)} && !window.location.hash) window.history.replaceState(null, "", "#o=" + ${JSON.stringify(server.info.once)});`),
      });
      await other.addInitScript((loc) => localStorage.setItem("hokma.locale", loc), locale);
      const tab = await other.newPage();
      await tab.goto(server.base);
      await tab.getByRole("button", { name: msg("nav.shutdown") }).waitFor();
      await sleep(500);

      const entry = await open();
      await entry.getByTestId("no-token").waitFor();
      await entry.getByTestId("no-token-shutdown").click();
      await entry.getByRole("alertdialog").getByRole("button", { name: msg("shutdown.confirm") }).click();
      await entry.getByTestId("shutdown-problem").waitFor();
      assert.equal(await entry.getByTestId("shutdown-problem").innerText(), msg("shutdown.inUse"));
      assert.ok(await alive(server.base), "Hokma stays up");
      assert.equal((await post(server)).status, 409, "the engine refuses without a session");
      assert.ok(await tab.getByRole("button", { name: msg("nav.shutdown") }).isVisible(), "the tab with a session is untouched");
      await other.close();
    }, { session: false });
    console.log(`ok shutdown ${locale}`);
  } catch (error) {
    failed = true;
    console.error(`FAIL shutdown ${locale}:`, error.stack || error);
  }
}
process.exit(failed ? 1 : 0);
