/**
 * Remembered session E2E (SPEC-001 addendum 0.0.6, tests R1-01 to R1-09, R1-S4, R1-S5, R1-S11 and R1-S12): the real
 * backend, the real compiled dashboard and a real browser with a persistent profile.
 *
 *   node e2e/resume.e2e.mjs [es|en]            Chromium (default), both locales when omitted
 *   E2E_BROWSER=firefox node e2e/resume.e2e.mjs  Firefox: the session is NOT remembered (R1-S11)
 *   E2E_BROWSER=msedge  node e2e/resume.e2e.mjs  Edge (needs Edge installed; set CHROMIUM_PATH if it is elsewhere)
 *
 * The engine listens on 127.0.0.1. The start link opens there, registers the owner and hands over to the engine's own
 * host name (`hokma.localhost:<port>`), where the page makes its device key. Chromium and Edge resolve `*.localhost`
 * to the loopback by themselves.
 *
 * Chromium and Edge:
 * R1-01  open with `#t=&o=`: the page ends on the engine's own host, inside, with no secret left in the address; the
 *        engine was given a public key (two 32 byte coordinates) and a new owner confirmation; F5 keeps the session
 *        (challenge, signature, resume) with another new confirmation that works for the owner calls
 * R1-02  close the tab, open another one on the host: inside
 * R1-03  close the whole browser, open the same profile on the host: inside
 * R1-06  Settings > General > close the session of this browser: the entry screen, the key is gone from IndexedDB,
 *        and F5 shows the entry screen again
 * R1-08  the engine's cookie is HttpOnly and not readable by the page; IndexedDB holds a non extractable private key
 *        and nothing else; no token, confirmation or code in localStorage or sessionStorage
 * R1-09  the entry screen never names `hokma start` or the terminal
 * R1-S4  a server on the same host name and ANOTHER port is sent the cookie by the browser; with that exact cookie it
 *        can get a challenge but cannot answer it: the engine refuses the resume
 * R1-S5  the origin of that server has no key (IndexedDB is per origin), and no signature of a made-up key is accepted
 *
 * Firefox:
 * R1-S11 no redirect, no cookie, no key, F5 leaves the session, and General shows the notice to use another browser
 *
 * Chromium without IndexedDB:
 * R1-S12 the page stays on 127.0.0.1, nothing is remembered, General shows the notice that the browser cannot keep the key
 */
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { loadPlaywright, free, sleep, startServer, translator, buildDashboard } from "./lib.mjs";

const browserName = process.env.E2E_BROWSER ?? "chromium";
const playwright = loadPlaywright();
const engine = browserName === "firefox" ? playwright.firefox : playwright.chromium;
const channel = browserName === "msedge" ? { channel: "msedge" } : {};
const launchOptions = {
  headless: true,
  ...channel,
  ...(process.env.CHROMIUM_PATH && browserName !== "firefox" ? { executablePath: process.env.CHROMIUM_PATH } : {}),
};
const locales = process.argv[2] ? [process.argv[2]] : ["es", "en"];
const dist = buildDashboard();
const HOST_NAME = /^hokma\.localhost$/;
const COORDINATE = /^[A-Za-z0-9_-]{43}$/;

async function openProfile(profile, locale, init) {
  const context = await engine.launchPersistentContext(profile, { ...launchOptions, viewport: { width: 1440, height: 940 } });
  await context.addInitScript((loc) => {
    if (!localStorage.getItem("hokma.locale")) localStorage.setItem("hokma.locale", loc);
  }, locale);
  if (init) await context.addInitScript(init);
  const page = context.pages()[0] ?? (await context.newPage());
  return { context, page };
}

/** What the tab sends to the engine, seen in its own requests and answers. */
function trackSession(page) {
  const seen = { nonce: null, token: null, startRegistrations: [], hostRegistrations: [], enters: [], challenges: [], resumes: [], handoffs: 0, codes: [] };
  page.on("request", (r) => {
    if (r.method() !== "POST") return;
    const url = new URL(r.url());
    const body = () => JSON.parse(r.postData() ?? "{}");
    if (url.pathname === "/api/session/owner-nonce") {
      (url.hostname === "127.0.0.1" ? seen.startRegistrations : seen.hostRegistrations).push(body());
    }
    if (url.pathname === "/api/session/handoff-code") seen.handoffs += 1;
    if (url.pathname === "/api/session/enter") {
      const sent = body();
      seen.enters.push(sent);
      seen.nonce = sent.nonce;
    }
    if (url.pathname === "/api/session/resume") {
      const sent = body();
      seen.resumes.push(sent);
      seen.nonce = sent.nonce;
    }
    const owner = r.headers()["x-hokma-owner"];
    if (owner) seen.nonce = owner;
  });
  page.on("response", async (res) => {
    const url = new URL(res.url());
    if (res.request().method() !== "POST") return;
    try {
      const data = await res.json();
      if (url.pathname === "/api/session/handoff-code" && typeof data.code === "string") seen.codes.push(data.code);
      if (url.pathname === "/api/session/challenge" && typeof data.challenge === "string") seen.challenges.push(data.challenge);
      if ((url.pathname === "/api/session/enter" || url.pathname === "/api/session/resume") && typeof data.token === "string") seen.token = data.token;
    } catch {
      /* not a JSON answer */
    }
  });
  return seen;
}

/** The record the page keeps in IndexedDB, as the page itself sees it. Call it only once the page made its database. */
const deviceKeyInfo = (page) =>
  page.evaluate(
    () =>
      new Promise((resolve, reject) => {
        const open = indexedDB.open("hokma-session");
        open.onerror = () => reject(open.error);
        open.onsuccess = () => {
          const db = open.result;
          const names = [...db.objectStoreNames];
          if (!names.includes("keys")) {
            db.close();
            resolve({ names, record: false });
            return;
          }
          const get = db.transaction("keys", "readonly").objectStore("keys").get("device");
          get.onerror = () => reject(get.error);
          get.onsuccess = async () => {
            const value = get.result;
            db.close();
            if (!value) {
              resolve({ names, record: false });
              return;
            }
            let readable = true;
            try {
              await crypto.subtle.exportKey("jwk", value.privateKey);
            } catch {
              readable = false;
            }
            resolve({
              names,
              record: true,
              fields: Object.keys(value).sort(),
              type: value.privateKey.type,
              extractable: value.privateKey.extractable,
              readable,
              algorithm: value.privateKey.algorithm.name,
              curve: value.privateKey.algorithm.namedCurve,
            });
          };
        };
      }),
  );

const databaseNames = (page) => page.evaluate(async () => (await indexedDB.databases()).map((d) => d.name));

/** A request with the Host header of the engine's own host name, as a process on this machine can make it. */
function rawRequest({ port, host, method, route, headers = {}, body }) {
  return new Promise((resolve, reject) => {
    const payload = body === undefined ? null : JSON.stringify(body);
    const req = http.request(
      {
        host: "127.0.0.1",
        port,
        method,
        path: route,
        headers: { Host: host, ...(payload ? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload) } : {}), ...headers },
      },
      (res) => {
        let data = "";
        res.on("data", (d) => (data += d));
        res.on("end", () => {
          let json = null;
          try {
            json = JSON.parse(data);
          } catch {
            /* not JSON */
          }
          resolve({ status: res.statusCode, json });
        });
      },
    );
    req.on("error", reject);
    if (payload) req.write(payload);
    req.end();
  });
}

/** Another server on the machine, on another port, that keeps the Cookie header of what it is sent. */
async function startListener() {
  const port = await free();
  const received = [];
  const server = http.createServer((req, res) => {
    received.push({ path: req.url, cookie: req.headers.cookie ?? "" });
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end("<!doctype html><title>other</title><p>other server</p>");
  });
  await new Promise((resolve) => server.listen(port, "127.0.0.1", resolve));
  return { port, received, stop: () => new Promise((resolve) => server.close(resolve)) };
}

async function scenario(locale) {
  const { M, msg } = translator(locale);
  const root = fs.mkdtempSync(path.join(os.tmpdir(), `hokma-resume-e2e-${browserName}-${locale}-`));
  const server = await startServer({ script: "permissions-serve.py", root, dist });
  const profile = path.join(root, "browser-profile");
  const inside = (page) => page.locator("#chat-message").waitFor({ timeout: 20000 });
  const entry = (page) => page.getByTestId("no-token").waitFor({ timeout: 20000 });
  const ownerCall = (nonce, mode) =>
    fetch(`${server.base}api/profile/permission-mode`, {
      method: "PUT",
      headers: { "X-Hokma-Token": server.token, "X-Hokma-Owner": nonce, "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
  const openGeneral = async (page) => {
    await page.getByRole("button", { name: M["nav.settings"] }).click();
    const dialog = page.getByRole("dialog", { name: msg("settings.title") });
    await dialog.getByRole("button", { name: msg("settings.general") }).click();
    return dialog;
  };
  let { context, page } = await openProfile(profile, locale);
  let other = null;
  try {
    const seen = trackSession(page);
    await page.goto(`${server.base}#t=${server.token}&o=${server.once}`);

    if (browserName === "firefox") {
      // R1-S11. no redirect, no cookie, no key; F5 leaves the session; General explains it
      await inside(page);
      assert.equal(new URL(page.url()).hostname, "127.0.0.1", "Firefox stays on the start link page");
      assert.equal(seen.handoffs, 0, "Firefox never asks for a handoff code");
      assert.equal(seen.startRegistrations.length >= 1, true, "the tab registers its confirmation");
      assert.deepEqual(await context.cookies(), [], "Firefox is not given a cookie");
      assert.deepEqual(await databaseNames(page), [], "Firefox keeps no key");
      const dialog = await openGeneral(page);
      await dialog.getByTestId("session-firefox").waitFor();
      assert.ok((await dialog.getByTestId("session-firefox").innerText()).includes(msg("session.firefoxNotice")));
      await page.getByRole("button", { name: msg("settings.close") }).click();
      await page.reload();
      await entry(page);
      assert.ok(!(await page.locator("body").innerText()).includes(msg("session.revokedNotice")), "leaving is not a revocation");
      return;
    }

    // R1-01. the start link ends on the engine's own host, inside, with the address clean
    await page.waitForURL((url) => HOST_NAME.test(url.hostname) && url.port === String(server.port), { timeout: 20000 });
    await inside(page);
    const hostUrl = new URL(page.url());
    const hostBase = `${hostUrl.protocol}//${hostUrl.host}/`;
    assert.equal(hostUrl.hash, "", "the handoff code leaves the address bar");
    assert.equal(seen.startRegistrations.length >= 1, true, "the start link page registered the owner");
    assert.equal(Object.hasOwn(seen.startRegistrations[0], "remember"), false, "no cookie is requested at the registration");
    assert.equal(seen.handoffs, 1, "one handoff code was asked for");
    assert.equal(seen.enters.length, 1, "the host page entered once");
    const enter = seen.enters[0];
    // the body of the handoff answer is gone once the page navigates, so the shape of the code is what can be checked
    assert.match(enter.code, /^[A-Z2-7]{16}$/, "it entered with a code of the engine");
    assert.match(enter.nonce, /^[0-9a-f]{32}$/, "a new 128 bit confirmation");
    assert.notEqual(enter.nonce, seen.startRegistrations[0].nonce, "not the one of the start link page");
    assert.match(enter.public_key.x, COORDINATE);
    assert.match(enter.public_key.y, COORDINATE);
    assert.deepEqual(Object.keys(enter).sort(), ["code", "nonce", "public_key"]);
    assert.equal((await ownerCall(enter.nonce, "ask")).status, 200, "the confirmation given at the entry works for the owner calls");

    // R1-08. the cookie and the key, read with the protocol of the browser and from the page
    const cdp = await context.newCDPSession(page);
    const { cookies } = await cdp.send("Network.getAllCookies");
    const engineCookies = cookies.filter((c) => c.domain.replace(/^\./, "") === hostUrl.hostname);
    assert.ok(engineCookies.length > 0, "the engine set its cookie on the host");
    assert.ok(engineCookies.every((c) => c.httpOnly), "every cookie of the engine is HttpOnly");
    assert.equal(await page.evaluate(() => document.cookie), "", "the page cannot read the cookie");
    const keyInfo = await deviceKeyInfo(page);
    assert.deepEqual(keyInfo.names, ["keys"]);
    assert.equal(keyInfo.record, true, "the device key is in IndexedDB");
    assert.deepEqual(keyInfo.fields, ["privateKey", "publicKey"]);
    assert.equal(keyInfo.type, "private");
    assert.equal(keyInfo.extractable, false, "the private key is not extractable");
    assert.equal(keyInfo.readable, false, "not even the page can read the private key");
    assert.equal(keyInfo.algorithm, "ECDSA");
    assert.equal(keyInfo.curve, "P-256");
    assert.deepEqual(await databaseNames(page), ["hokma-session"], "the key is the only use of IndexedDB");
    const storages = await page.evaluate(() => JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)]));
    for (const secret of [server.token, enter.nonce, enter.code, server.once, ...engineCookies.map((c) => c.value)]) {
      assert.ok(!storages.includes(secret), "no storage of the page holds a secret");
    }

    // R1-01 (cont.). F5 keeps the session through challenge, signature and resume, with another new confirmation
    await page.reload();
    await inside(page);
    assert.equal(await page.getByTestId("no-token").count(), 0, "no entry screen after F5");
    await sleep(500);
    assert.equal(seen.enters.length, 1, "F5 does not enter again");
    assert.equal(seen.resumes.length, 1, "F5 resumed with the key");
    const resumed = seen.resumes[0];
    assert.deepEqual(Object.keys(resumed).sort(), ["challenge", "nonce", "signature"]);
    assert.ok(seen.challenges.includes(resumed.challenge), "it answered a challenge the engine gave");
    assert.match(resumed.signature, /^[A-Za-z0-9_-]{86}$/, "a raw r||s signature of 64 bytes in base64url");
    assert.match(resumed.nonce, /^[0-9a-f]{32}$/, "a new 128 bit confirmation");
    assert.notEqual(resumed.nonce, enter.nonce);
    assert.equal(seen.hostRegistrations.length, 0, "the host page never registers the owner by the old path after F5");
    assert.equal((await ownerCall(resumed.nonce, "ask")).status, 200, "the confirmation of the new load works for the owner calls");
    assert.equal((await ownerCall("0".repeat(32), "auto")).status, 403, "a value the tab did not register is refused");

    // R1-02. a new tab on the host
    const second = await context.newPage();
    await second.goto(hostBase);
    await inside(second);
    await second.close();

    // R1-03. the whole browser closed and opened again on the same profile
    await page.close();
    await context.close();
    ({ context, page } = await openProfile(profile, locale));
    const again = trackSession(page);
    await page.goto(hostBase);
    await inside(page);
    assert.equal(await page.getByTestId("no-token").count(), 0);
    await sleep(500);
    assert.equal(again.resumes.length, 1, "the reopened browser resumed with the key");
    assert.equal((await ownerCall(again.nonce, "ask")).status, 200, "the reopened browser is the owner too");

    // R1-S4. a server on the same host name and ANOTHER port is sent the cookie by the browser
    other = await startListener();
    const thiefPage = await context.newPage();
    await thiefPage.goto(`http://${hostUrl.hostname}:${other.port}/api/session/resume`);
    const caught = other.received.map((r) => r.cookie).find((c) => c.length > 0);
    assert.ok(caught, "the browser sent the cookie to the other port with the same name (cookies do not know ports)");
    const common = {
      port: server.port,
      host: hostUrl.host,
      headers: { Cookie: caught, Origin: hostUrl.origin, "Sec-Fetch-Site": "same-origin" },
    };
    const challenge = await rawRequest({ ...common, method: "POST", route: "/api/session/challenge" });
    const forged = crypto.randomBytes(64).toString("base64url");
    const stolen = await rawRequest({
      ...common,
      method: "POST",
      route: "/api/session/resume",
      body: { nonce: "a".repeat(32), challenge: challenge.json?.challenge ?? "x".repeat(32), signature: forged },
    });
    assert.equal(stolen.status, 401, "the exact cookie is not enough: the challenge cannot be signed without the key");
    assert.equal(stolen.json?.token, undefined, "no token is given");
    assert.equal((await ownerCall("a".repeat(32), "auto")).status, 403, "the confirmation of the thief is not the owner's");

    // R1-S5. the origin of that server holds no key, and a signature by a key the engine does not know is refused too
    assert.deepEqual(await databaseNames(thiefPage), [], "IndexedDB is per origin: the other port has no key");
    const own = crypto.generateKeyPairSync("ec", { namedCurve: "prime256v1" });
    const message = `hokma-resume:${hostUrl.origin}:${challenge.json?.challenge ?? ""}:${"b".repeat(32)}`;
    const signature = crypto.sign("sha256", Buffer.from(message), { key: own.privateKey, dsaEncoding: "ieee-p1363" }).toString("base64url");
    const unknownKey = await rawRequest({
      ...common,
      method: "POST",
      route: "/api/session/resume",
      body: { nonce: "b".repeat(32), challenge: challenge.json?.challenge ?? "", signature },
    });
    assert.equal(unknownKey.status, 401, "a signature by a key the engine never saw is refused");
    await thiefPage.close();

    // the legitimate tab is not affected by any of it
    await page.reload();
    await inside(page);

    // R1-09. the entry screen, reached by closing the session
    const dialog = await openGeneral(page);
    await dialog.getByRole("button", { name: msg("session.logout.button") }).click();
    await entry(page);
    const text = (await page.locator("body").innerText()).toLowerCase();
    assert.ok(!text.includes("hokma start"), "the entry screen does not name hokma start");
    assert.ok(!/terminal/.test(text), "the entry screen does not send anyone to a terminal");
    assert.ok(text.includes(msg("noToken.title").toLowerCase()));
  } finally {
    await other?.stop().catch(() => undefined);
    await context.close().catch(() => undefined);
    await server.stop();
  }
}

/** R1-06 needs a session the previous scenario spent, so it runs on its own server and profile. */
async function logoutScenario(locale) {
  const { M, msg } = translator(locale);
  const root = fs.mkdtempSync(path.join(os.tmpdir(), `hokma-resume-logout-e2e-${locale}-`));
  const server = await startServer({ script: "permissions-serve.py", root, dist });
  const { context, page } = await openProfile(path.join(root, "browser-profile"), locale);
  try {
    await page.goto(`${server.base}#t=${server.token}&o=${server.once}`);
    await page.waitForURL((url) => HOST_NAME.test(url.hostname), { timeout: 20000 });
    await page.locator("#chat-message").waitFor({ timeout: 20000 });
    assert.equal((await deviceKeyInfo(page)).record, true, "the key is there before closing");
    await page.getByRole("button", { name: M["nav.settings"] }).click();
    const dialog = page.getByRole("dialog", { name: msg("settings.title") });
    await dialog.getByRole("button", { name: msg("settings.general") }).click();
    await dialog.getByRole("button", { name: msg("session.logout.button") }).click();
    await page.getByTestId("no-token").waitFor({ timeout: 20000 });
    assert.equal((await deviceKeyInfo(page)).record, false, "closing the session deletes the key");
    await page.reload();
    await page.getByTestId("no-token").waitFor({ timeout: 20000 });
    const cookies = await context.cookies();
    assert.equal(cookies.filter((c) => HOST_NAME.test(c.domain.replace(/^\./, "")) && c.value).length, 0, "the cookie is gone");
    assert.equal(await page.getByTestId("session-revoked").count(), 0, "closing it on purpose is not a revocation");
  } finally {
    await context.close().catch(() => undefined);
    await server.stop();
  }
}

/** R1-S12. A browser that cannot keep the key: nothing is remembered and nothing breaks. */
async function noStorageScenario(locale) {
  const { M, msg } = translator(locale);
  const root = fs.mkdtempSync(path.join(os.tmpdir(), `hokma-resume-nostorage-e2e-${locale}-`));
  const server = await startServer({ script: "permissions-serve.py", root, dist });
  const { context, page } = await openProfile(path.join(root, "browser-profile"), locale, () => {
    Object.defineProperty(window, "indexedDB", { value: undefined, configurable: true });
  });
  try {
    const seen = trackSession(page);
    await page.goto(`${server.base}#t=${server.token}&o=${server.once}`);
    await page.locator("#chat-message").waitFor({ timeout: 20000 });
    await sleep(500);
    assert.equal(new URL(page.url()).hostname, "127.0.0.1", "the page stays on the start link page");
    assert.equal(seen.handoffs, 0, "it never asks for a handoff code");
    assert.deepEqual(await context.cookies(), [], "no cookie");
    await page.getByRole("button", { name: M["nav.settings"] }).click();
    const dialog = page.getByRole("dialog", { name: msg("settings.title") });
    await dialog.getByRole("button", { name: msg("settings.general") }).click();
    await dialog.getByTestId("session-no-storage").waitFor();
    assert.ok((await dialog.getByTestId("session-no-storage").innerText()).includes(msg("session.noStorage")));
    await page.getByRole("button", { name: msg("settings.close") }).click();
    await page.reload();
    await page.getByTestId("no-token").waitFor({ timeout: 20000 });
  } finally {
    await context.close().catch(() => undefined);
    await server.stop();
  }
}

let failed = false;
for (const locale of locales) {
  try {
    await scenario(locale);
    if (browserName !== "firefox") {
      await logoutScenario(locale);
      await noStorageScenario(locale);
    }
    console.log(`ok resume ${locale}`);
  } catch (error) {
    failed = true;
    console.error(`FAIL resume ${locale}:`, error.stack || error);
  }
}
process.exit(failed ? 1 : 0);
