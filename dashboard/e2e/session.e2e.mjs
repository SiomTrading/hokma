/**
 * Session E2E (bugs 1035 and 1038): what a browser profile on disk holds after Hokma was used, against the real
 * backend, the real compiled dashboard and a real Chromium with a persistent profile.
 *
 *   node e2e/session.e2e.mjs
 *
 * The start link opens on 127.0.0.1, registers the owner and hands over to the engine's own host name
 * (`hokma.localhost:<port>`), where the page makes its device key and enters with a one-time code.
 *
 * S1  after opening Hokma with `#t=`/`#o=`, using it, letting it restart by itself (the automatic restart that
 *     follows an update, the one reload that keeps the session), reloading it with F5 (which loses the session and
 *     comes back through the device key, leaving nothing in window.name) and then CLOSING THE TAB with the browser
 *     still alive, and closing the browser, the session token, every owner confirmation, the one-time code, the
 *     handoff code and the challenges are in NO file of the whole profile (Session Storage, Local Storage, Sessions,
 *     History, cookies, IndexedDB, caches, everything; UTF-8 and UTF-16LE).
 *     What the profile DOES hold is the device key: IndexedDB `hokma-session`, store `keys`, key `device`, a
 *     non extractable ECDSA key pair. Its material lives in the profile. That protects against servers on this machine
 *     and against a cookie that was copied (the cookie alone cannot sign a challenge), and it does not protect against
 *     someone who can read the disk of the user, who can take the profile as a whole. The scan does not look for it.
 * S2  the token alone (what an agent could read) is refused by the owner calls with the catalog code, and another
 *     confirmation cannot take the tab's place
 * S3  positive controls: the same scan DOES find a value kept in sessionStorage, in localStorage and in IndexedDB, so
 *     "found in none" means something. The start link origin (127.0.0.1) never creates an IndexedDB.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { buildDashboard, loadPlaywright, sleep, startServer, until } from "./lib.mjs";

function filesUnder(dir) {
  const out = [];
  const walk = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) walk(p);
      else if (e.isFile()) out.push(p);
    }
  };
  walk(dir);
  return out;
}

/** The files holding `needle` as plain text or as UTF-16 (how Chrome writes the values of a page's storage). */
function containing(dir, needle) {
  const encodings = [Buffer.from(needle, "utf8"), Buffer.from(needle, "utf16le")];
  return filesUnder(dir).filter((f) => {
    try {
      const data = fs.readFileSync(f);
      return encodings.some((e) => data.includes(e));
    } catch {
      return false;
    }
  });
}

const launchOptions = { headless: true, ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}) };
const HOST_NAME = /^hokma\.localhost$/;

/** Everything the page can see of its own storage (except the device key), as one string. */
const storageOf = (page) =>
  page.evaluate(() => JSON.stringify([Object.entries(sessionStorage), Object.entries(localStorage), document.cookie]));

const databaseNames = (page) => page.evaluate(async () => (await indexedDB.databases()).map((d) => d.name));

/**
 * What a user does with F5: the key reaches the page first, then the browser reloads it (a browser-initiated reload).
 * Playwright's key events do not make the browser reload by themselves, so the reload is requested right after.
 * Nothing is handed over by it: the new page has no token and comes back through the device key.
 */
async function reloadWithKeyboard(page) {
  await page.locator("body").press("F5");                                    // the page has the focus, as it has for a person
  await sleep(150);                                                          // a person is never faster than this
  await page.evaluate(() => {
    window.dispatchEvent(new Event("beforeunload"));
    window.dispatchEvent(new Event("pagehide"));
  });
  assert.equal(await page.evaluate(() => window.name), "", "the key and the leave events write nothing in window.name");
  await page.reload();
}

const { chromium } = loadPlaywright();
const built = buildDashboard();
const root = fs.mkdtempSync(path.join(os.tmpdir(), "hokma-session-e2e-"));
const server = await startServer({ script: "permissions-serve.py", root, dist: built });
const profile = path.join(root, "browser-profile");
let failed = false;
let context;
try {
  context = await chromium.launchPersistentContext(profile, { ...launchOptions, viewport: { width: 1440, height: 940 } });
  const page = context.pages()[0] ?? (await context.newPage());

  // the owner confirmations are made in the page; their values are seen here in the requests that carry them
  const startRegistrations = [];                    // the start link page, on 127.0.0.1
  const hostRegistrations = [];                     // the engine's own host, after the automatic restart
  const enters = [];
  const resumes = [];
  const handoffCodes = [];
  const challenges = [];
  page.on("request", (r) => {
    if (r.method() !== "POST") return;
    const url = new URL(r.url());
    const body = () => JSON.parse(r.postData() ?? "{}");
    if (url.pathname === "/api/session/owner-nonce") (url.hostname === "127.0.0.1" ? startRegistrations : hostRegistrations).push(body());
    if (url.pathname === "/api/session/enter") enters.push(body());
    if (url.pathname === "/api/session/resume") resumes.push(body());
  });
  page.on("response", async (res) => {
    if (res.request().method() !== "POST") return;
    const pathname = new URL(res.url()).pathname;
    try {
      const data = await res.json();
      if (pathname === "/api/session/handoff-code" && typeof data.code === "string") handoffCodes.push(data.code);
      if (pathname === "/api/session/challenge" && typeof data.challenge === "string") challenges.push(data.challenge);
    } catch {
      /* not a JSON answer */
    }
  });
  // the sockets of the engine and /health go through routes from the first load, to let the engine "restart" later
  let down = false;
  const sockets = [];
  await page.routeWebSocket(/\/ws\//, (ws) => {
    ws.connectToServer();
    sockets.push(ws);
  });
  await page.route("**/health", (route) => (down ? route.abort() : route.continue()));
  await page.goto(`${server.base}#t=${server.token}&o=${server.once}`);
  await page.waitForURL((url) => HOST_NAME.test(url.hostname) && url.port === String(server.port), { timeout: 20000 });
  await page.locator("#chat-message").waitFor();
  assert.equal(startRegistrations.length >= 1, true, "the start link page registers its confirmation by itself");
  assert.match(startRegistrations[0].nonce ?? "", /^[0-9a-f]{32}$/, "the start link page made a 128 bit confirmation");
  assert.equal(enters.length, 1, "the engine's own host entered once");
  const nonce = enters[0].nonce;
  assert.match(nonce ?? "", /^[0-9a-f]{32}$/, "the host page made a new 128 bit confirmation");
  assert.notEqual(nonce, startRegistrations[0].nonce, "it is not the one of the start link page");
  assert.equal(await page.evaluate(() => window.location.hash), "", "the token, the one-time code and the handoff code leave the address bar");
  assert.equal(await page.evaluate(() => window.name), "", "nothing is handed over while the page is open");

  const secrets = [server.token, startRegistrations[0].nonce, nonce, server.once, ...handoffCodes];
  const inStorage = await storageOf(page);
  for (const secret of secrets) assert.ok(!inStorage.includes(secret), "no storage of the tab holds a secret");
  assert.equal(await page.evaluate(() => sessionStorage.length), 0, "sessionStorage is empty");
  assert.equal(await page.evaluate(() => document.cookie.length), 0, "no cookie the page can read");
  assert.deepEqual(await databaseNames(page), ["hokma-session"], "the device key is the only use of IndexedDB");

  // S2. the token alone is not enough for the owner calls
  const call = (method, route, headers, body) =>
    fetch(`${server.base}${route}`, {
      method,
      headers: { "X-Hokma-Token": server.token, "Content-Type": "application/json", ...headers },
      body: JSON.stringify(body),
    });
  const refused = await call("PUT", "api/profile/permission-mode", {}, { mode: "auto" });
  assert.equal(refused.status, 403);
  assert.equal((await refused.json()).detail.code, "owner_confirmation_required");
  assert.equal((await server.api("/api/profile/permission-mode")).mode, "ask", "the mode did not change");
  const wrong = await call("PUT", "api/profile/permission-mode", { "X-Hokma-Owner": "0".repeat(32) }, { mode: "auto" });
  assert.equal(wrong.status, 403, "a value the tab did not register");
  for (const route of ["api/trash/empty"]) {
    assert.equal((await call("POST", route, {}, { confirm: true })).status, 403, route);
  }
  assert.equal((await call("POST", "api/trash/1/purge", {}, { confirm: true })).status, 403);
  assert.equal((await call("POST", "api/trash/1/restore", {}, {})).status, 403);
  assert.equal((await call("POST", "api/checkpoints/x/restore-deleted", {}, {})).status, 403);
  const other = await call("POST", "api/session/owner-nonce", {}, { nonce: "f".repeat(32) });
  assert.equal(other.status, 409, "nobody else can take the place of the tab");
  assert.equal((await other.json()).detail.code, "owner_already_registered");
  const reused = await call("POST", "api/session/owner-nonce", {}, { nonce: "f".repeat(32), once: server.once });
  assert.equal(reused.status, 409, "the one-time code is spent with the first registration");

  // S1a. the automatic restart of an update: the engine's socket closes with the reason `restarting`, the engine is
  // unreachable for a moment, then back; the page reloads by itself through reloadWithHandoff(). Same tab, same
  // session, same confirmation, and the engine gets it back with a fresh challenge and signature.
  await until(() => sockets.length > 0, "a socket of the engine", 15000);
  await page.evaluate(() => {
    window.__beforeRestart = true;
  });
  down = true;
  setTimeout(() => (down = false), 3500);
  for (const ws of sockets) ws.close({ code: 1012, reason: "restarting" });  // what the engine does when it goes down to restart
  await until(async () => (await page.evaluate(() => window.__beforeRestart ?? false)) === false, "the page restarted by itself", 40000);
  await page.locator("#chat-message").waitFor();
  assert.equal(await page.evaluate(() => window.name), "", "emptied after the automatic restart too");
  assert.equal(await page.evaluate(() => sessionStorage.length), 0);
  assert.ok(!(await storageOf(page)).includes(server.token));
  assert.ok(hostRegistrations.every((r) => r.nonce === nonce), "still the same confirmation after the restart");
  await until(() => resumes.length >= 1, "the signature after the restart", 20000);
  assert.equal(resumes[0].nonce, nonce, "the restart gives the engine the same confirmation back");
  assert.equal(enters.length, 1, "the restart does not enter again");
  await page.unrouteAll({ behavior: "ignoreErrors" });

  // S1b. F5 (and Ctrl+R, and the toolbar button) hand nothing over through the page: the session comes back only
  // through the device key (resume.e2e.mjs covers that in depth). Nothing of the session is left in window.name or in
  // any storage, and the page makes a NEW confirmation instead of reusing a stored one.
  const resumesBeforeF5 = resumes.length;
  const registrationsBeforeF5 = hostRegistrations.length;
  await reloadWithKeyboard(page);
  await page.locator("#chat-message").waitFor();
  assert.equal(await page.evaluate(() => window.name), "", "nothing is left in window.name after F5");
  assert.equal(await page.evaluate(() => sessionStorage.length), 0);
  await until(() => resumes.length > resumesBeforeF5, "the resume after F5", 20000);
  const afterF5 = resumes[resumes.length - 1];
  assert.match(afterF5.nonce, /^[0-9a-f]{32}$/);
  assert.notEqual(afterF5.nonce, nonce, "F5 made a new confirmation");
  assert.equal(hostRegistrations.length, registrationsBeforeF5, "F5 does not register the owner by the one-time path");
  const stored = await storageOf(page);
  for (const secret of [...secrets, afterF5.nonce, afterF5.challenge, afterF5.signature]) {
    assert.ok(!stored.includes(secret), "no secret in a storage after F5");
  }
  secrets.push(afterF5.nonce);

  // S1c. close the TAB while the browser lives (this is what dumps sessionStorage into the profile), then the browser
  await sleep(500);
  await page.close();
  await sleep(1500);
  await context.close();
  context = undefined;
  const files = filesUnder(profile);
  assert.ok(files.length > 5, "the profile is a real one");
  for (const resumed of resumes) secrets.push(resumed.nonce);
  const everything = [
    ["session token", server.token],
    ["one-time code", server.once],
    ...secrets.filter((s) => s !== server.token && s !== server.once).map((s) => ["owner confirmation or code", s]),
    ...challenges.map((c) => ["challenge", c]),
    ...resumes.map((r) => ["signature", r.signature]),
  ];
  for (const [what, secret] of everything) {
    const hits = containing(profile, secret);
    assert.deepEqual(hits, [], `the ${what} is in the browser profile: ${hits.join(", ")}`);
  }

  // the start link origin never keeps anything: a probe of its own origin finds no IndexedDB at all
  context = await chromium.launchPersistentContext(profile, { ...launchOptions, viewport: { width: 1440, height: 940 } });
  const probeStart = context.pages()[0] ?? (await context.newPage());
  await probeStart.goto(`${server.base}health`);
  assert.deepEqual(await databaseNames(probeStart), [], "127.0.0.1 never creates an IndexedDB");
  await context.close();
  context = undefined;

  // S3. controls: the same close-the-tab-then-scan path DOES see sessionStorage, localStorage and IndexedDB
  const control = path.join(root, "control-profile");
  const markerSession = `CONTROL-SESSION-${Date.now()}-MARKER`;
  const markerLocal = `CONTROL-LOCAL-${Date.now()}-MARKER`;
  const markerIdb = `CONTROL-IDB-${Date.now()}-MARKER`;
  context = await chromium.launchPersistentContext(control, launchOptions);
  const keeper = context.pages()[0] ?? (await context.newPage());      // keeps the browser alive while the tab closes
  const probe = await context.newPage();
  await probe.goto(`${server.base}health`);
  await probe.evaluate(
    ([a, b]) => {
      sessionStorage.setItem("control", a);
      localStorage.setItem("control", b);
    },
    [markerSession, markerLocal],
  );
  await probe.evaluate(
    (marker) =>
      new Promise((resolve, reject) => {
        const open = indexedDB.open("control", 1);
        open.onupgradeneeded = () => open.result.createObjectStore("s");
        open.onerror = () => reject(open.error);
        open.onsuccess = () => {
          const db = open.result;
          const tx = db.transaction("s", "readwrite");
          tx.objectStore("s").put(marker, "k");
          tx.oncomplete = () => {
            db.close();
            resolve(true);
          };
          tx.onerror = () => reject(tx.error);
        };
      }),
    markerIdb,
  );
  await sleep(500);
  await probe.close();
  await sleep(4000);
  void keeper;
  await context.close();
  context = undefined;
  assert.ok(containing(control, markerSession).length > 0, "the scan sees a value kept in sessionStorage");
  assert.ok(containing(control, markerLocal).length > 0, "the scan sees a value kept in localStorage");
  assert.ok(containing(control, markerIdb).length > 0, "the scan sees a value kept in IndexedDB");
  console.log(`${files.length} profile files scanned (tab closed with the browser alive), the token, the confirmations, the codes and the challenges are in none`);
  for (const locale of ["es", "en"]) console.log(`ok session ${locale}`); // the checks do not depend on the language
} catch (error) {
  failed = true;
  console.error("FAIL session:", error.stack || error);
} finally {
  if (context) await context.close().catch(() => undefined);
  await server.stop();
}
process.exit(failed ? 1 : 0);
