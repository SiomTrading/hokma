/**
 * Conversations E2E (task 3083, Hokma 0.0.7): "New conversation" opens a new conversation for real, one conversation per
 * browser tab, every conversation with its own address, and every new conversation starts in General (the working
 * folder) unless a project is picked. Also: General in the projects panel, and more than one project.
 *
 *   node e2e/conversations.e2e.mjs [es|en]        (both locales when omitted)
 *
 * The real backend, the real compiled dashboard, a real Chromium and the fake `claude` first on PATH. A second
 * browser tab is a second page of the same browser context (it has its own sessionStorage, as a real one has).
 * A new page gets its session the way a handoff gives it (the device key does that in a real browser).
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { repo, reloadWithHandoff, runSuite, trackSession, until } from "./lib.mjs";

const starts = (log) =>
  fs.existsSync(log) ? fs.readFileSync(log, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)).filter((e) => e.event === "start") : [];
const real = (p) => fs.realpathSync(p);

await runSuite("conversations", { script: "serve.py" }, async ({ page: A, server, msg }) => {
  const ctx = A.context();
  const home = real(server.home);
  const folder = (slug) => path.join(home, "projects", slug);
  const cwds = () => starts(server.fakeLog).map((e) => real(e.cwd));
  const hash = (page) => page.evaluate(() => window.location.hash);
  // chat screens stay mounted in the background (keepAlive): everything is read from the one in front
  const front = (page) => page.locator(".screen-host:not([hidden])");
  const box = (page) => front(page).locator("#chat-message");
  const head = (page) => front(page).locator(".chat-head");
  const users = (page) => front(page).getByTestId("user-message");
  const answers = (page) => front(page).getByTestId("assistant-message");
  const newLink = (page) => page.getByRole("link", { name: msg("nav.newChat") });
  const scope = (page) => front(page).getByRole("radiogroup", { name: msg("chat.scope.label") });
  const tabsOf = (page) => page.locator(".top-tabs .tab");
  const nav = (name) => A.getByRole("complementary", { name: msg("nav.menu") }).getByRole("button", { name, exact: true });
  const send = async (page, text) => {
    const before = await users(page).count();
    await box(page).fill(text);
    await box(page).press("Enter");
    await until(async () => (await users(page).count()) > before, "the user bubble");
  };
  const idle = (page) => page.getByRole("button", { name: msg("chat.composer.send") }).waitFor({ timeout: 20000 });
  const done = async (page, n) => {
    await until(async () => (await answers(page).count()) >= n, `answer ${n}`);
    await idle(page);
  };
  const panel = A.getByRole("complementary", { name: msg("rightPanel.title") });
  const projectButton = (name) => panel.getByRole("list", { name: msg("projects.list") }).getByRole("button", { name });
  const general = () => panel.getByTestId("project-general");
  const secondPage = async (action) => {
    const [page] = await Promise.all([ctx.waitForEvent("page"), action()]);
    trackSession(page, server.token);
    await box(page).waitFor();
    return page;
  };

  // two more projects besides "principal" (active) and "Strategy"
  await server.api("/api/projects", { method: "POST", body: JSON.stringify({ name: "Beta", kind: "robot" }) });
  await A.goto(server.base);
  await box(A).waitFor();
  await projectButton(/Principal|E2E Fund/).waitFor();
  assert.equal((await server.api("/api/projects")).active_id !== null, true, "a project is active at the start");

  // E-01: with a project active, a new conversation is General, the address follows it and the list shows it
  await newLink(A).click();
  await scope(A).waitFor();
  assert.equal(await A.getByRole("radio", { name: msg("chat.scope.general") }).getAttribute("aria-checked"), "true");
  assert.equal(await hash(A), "#/chat/new");
  await head(A).getByText(msg("chat.scope.general"), { exact: true }).waitFor();
  await send(A, "uno");
  await done(A, 1);
  await until(async () => /^#\/chat\/\d+$/.test(await hash(A)), "the address of the conversation");
  const id1 = (await hash(A)).split("/").pop();
  await head(A).getByText(msg("chat.scope.general"), { exact: true }).waitFor();
  assert.equal(await scope(A).count(), 0, "the picker is gone after the first message");
  assert.equal(cwds().at(-1), home, "the first turn ran in the working folder, not in the active project");
  await A.locator("aside .conv", { hasText: "uno" }).waitFor();

  // E-02: another new conversation: a clean draft with the picker; the first one stays in its own tab with its history
  await newLink(A).click();
  await scope(A).waitFor();
  assert.equal(await users(A).count(), 0);
  assert.equal(await hash(A), "#/chat/new");
  await A.locator(".top-tabs").getByText("uno", { exact: true }).click();
  await until(async () => (await hash(A)) === `#/chat/${id1}`, "the address back on the first conversation");
  assert.ok((await users(A).first().innerText()).includes("uno"));
  assert.ok((await answers(A).first().innerText()).includes("pong"));

  // E-03: "Chat" in the bar goes back to the last chat tab and empties none
  await nav(msg("nav.panel")).click();
  await nav(msg("nav.chat")).click();
  await until(async () => (await hash(A)) === `#/chat/${id1}`, "Chat goes to the last chat tab");
  assert.equal(await users(A).count(), 1);

  // E-04: Ctrl+click on "New conversation" opens another browser tab: its own draft, signed in, General marked
  const B = await secondPage(() => newLink(A).click({ modifiers: ["Control"] }));
  assert.equal(await hash(B), "#/chat/new");
  await scope(B).waitFor();
  assert.equal(await B.getByRole("radio", { name: msg("chat.scope.general") }).getAttribute("aria-checked"), "true");
  assert.equal(await B.locator("text=" + msg("noToken.title")).count(), 0, "no sign-in screen in the new browser tab");
  assert.equal(await tabsOf(B).count(), 1, "the new browser tab has only its own panel tab, not the ones of the first");
  assert.equal(await hash(A), `#/chat/${id1}`, "the first browser tab did not move");

  // E-05: pick Strategy in B and write; write in A at the same time: each answer goes to its own conversation
  await B.getByRole("radio", { name: "Strategy" }).click();
  await head(B).getByText("Strategy", { exact: true }).waitFor();
  await Promise.all([send(B, "dos"), send(A, "tres")]);
  await Promise.all([done(B, 1), done(A, 2)]);
  await until(async () => /^#\/chat\/\d+$/.test(await hash(B)), "the address of the conversation of B");
  const id2 = (await hash(B)).split("/").pop();
  assert.notEqual(id2, id1);
  assert.equal(await hash(A), `#/chat/${id1}`);
  assert.equal(await users(A).count(), 2);
  assert.equal(await users(B).count(), 1);
  assert.ok((await users(B).first().innerText()).includes("dos"));
  await head(B).getByText("Strategy", { exact: true }).waitFor();
  assert.ok(cwds().includes(folder("strategy")), "B ran in the project it picked");
  assert.equal((await server.api("/api/projects")).projects.find((p) => p.active).slug, "principal", "picking a project in a new conversation does not change the active project of the panel");

  // E-06: F5 in A and in B: each comes back to its conversation, neither takes the panel tabs of the other
  await reloadWithHandoff(A);
  await reloadWithHandoff(B);
  await box(A).waitFor();
  await box(B).waitFor();
  await until(async () => (await users(A).count()) === 2, "A history after the reload");
  await until(async () => (await users(B).count()) === 1, "B history after the reload");
  assert.equal(await hash(A), `#/chat/${id1}`);
  assert.equal(await hash(B), `#/chat/${id2}`);
  assert.equal(await tabsOf(B).count(), 1);
  assert.equal(await A.locator(".top-tabs").getByText("dos", { exact: true }).count(), 0, "A does not show the tab of B");

  // E-07: in A open the chart and close the tab of the first conversation; F5 in B changes nothing
  await nav(msg("nav.chart")).click();
  await A.getByRole("button", { name: msg("top.closeTab", { title: "uno" }) }).click();
  await reloadWithHandoff(B);
  await box(B).waitFor();
  await until(async () => (await users(B).count()) === 1, "B after the reload");
  assert.equal(await hash(B), `#/chat/${id2}`);
  assert.equal(await tabsOf(B).count(), 1);

  // E-08: wheel click on a row of the list opens that conversation in a new browser tab
  const C = await secondPage(() => A.locator("aside .conv", { hasText: "uno" }).click({ button: "middle" }));
  assert.equal(await hash(C), `#/chat/${id1}`);
  await until(async () => (await users(C).count()) === 2, "the history in the new browser tab");
  await C.close();

  // E-09: Ctrl+Shift+O opens a draft in the same browser tab, also from the terminal; Ctrl+N does nothing
  await nav(msg("nav.chat")).click();
  const pagesBefore = ctx.pages().length;
  const tabCount = await tabsOf(A).count();
  await A.keyboard.press("Control+n");
  assert.equal(await tabsOf(A).count(), tabCount, "Ctrl+N does nothing inside the page");
  await A.keyboard.press("Control+Shift+O");
  await scope(A).waitFor();
  assert.equal(await tabsOf(A).count(), tabCount + 1);
  assert.equal(await hash(A), "#/chat/new");
  const dock = A.getByRole("region", { name: msg("terminal.title") });
  await dock.getByRole("group", { name: msg("term.screen", { n: 1 }) }).click();
  await A.keyboard.press("Control+Shift+O");
  await until(async () => (await tabsOf(A).count()) === tabCount + 2, "a draft from the terminal");
  assert.equal(ctx.pages().length, pagesBefore, "no browser tab was opened by the shortcut");

  // E-11: the icon (an address without hash) in a new browser tab: the conversations of the last picture plus a new draft
  const D = await secondPage(async () => {
    const page = await ctx.newPage();
    trackSession(page, server.token);
    await page.goto(server.base);
  });
  await scope(D).waitFor();
  assert.ok((await tabsOf(D).count()) >= 2, "the last picture comes back");
  assert.equal(await hash(D), "#/chat/new");
  await head(D).getByText(msg("chat.scope.general"), { exact: true }).waitFor();
  await D.close();

  // More than one project (point 4): Strategy, Beta and General. Old chat, new chat and terminal each time.
  const seed = `
from hokma.db import open_database
db = open_database()
with db.write() as u:
    c = u.conversations.create(engine="claude_code", title="vieja", model="sonnet", project_mode="follow")
    u.messages.add(c.id, "user", "hola vieja")
    u.messages.add(c.id, "assistant", "pong", model="sonnet")
print(c.id)
`;
  const made = spawnSync("uv", ["run", "--project", repo, "python", "-c", seed], {
    cwd: repo,
    encoding: "utf8",
    env: { ...process.env, HOKMA_HOME: server.home, HOKMA_DATA_DIR: server.data },
  });
  assert.equal(made.status, 0, made.stderr);
  const oldId = made.stdout.trim().split("\n").pop();
  await reloadWithHandoff(A);
  await box(A).waitFor();

  const terminalRows = (n) => dock.getByRole("group", { name: msg("term.screen", { n }) }).locator(".xterm-rows");
  const where = async (n, tag) => {
    await dock.getByRole("group", { name: msg("term.screen", { n }) }).click();
    await A.keyboard.type(`echo ${tag}=$(pwd)`);
    await A.keyboard.press("Enter");
    await until(async () => new RegExp(`${tag}=/\\S+`).test((await terminalRows(n).innerText()).replace(/ /g, " ")), `pwd of terminal ${n}`);
    const line = (await terminalRows(n).innerText()).replace(/ /g, " ").match(new RegExp(`${tag}=(/\\S+)`));
    return real(line[1]);
  };
  const newTerminal = async (n) => {
    await dock.getByRole("button", { name: msg("terminal.new") }).click();
    await terminalRows(n).waitFor();
    await until(async () => (await terminalRows(n).innerText()).trim().length > 0, `prompt ${n}`);
  };
  const oldChat = async (text, expectCwd) => {
    await A.locator("aside .conv", { hasText: "vieja" }).click();
    await until(async () => (await hash(A)) === `#/chat/${oldId}`, "the old conversation");
    assert.equal(await scope(A).count(), 0, "an old conversation never shows the picker");
    await send(A, text);
    await idle(A);
    assert.equal(cwds().at(-1), expectCwd, `old chat "${text}" ran in ${expectCwd}`);
  };
  const newChat = async (text) => {
    await newLink(A).click();
    await scope(A).waitFor();
    await send(A, text);
    await idle(A);
    return cwds().at(-1);
  };

  await projectButton(/Strategy/).click();
  await A.getByText(msg("projects.toast.active", { name: "Strategy" })).first().waitFor();
  await oldChat("s1", folder("strategy"));
  assert.equal(await newChat("n1"), home, "new chat with Strategy active: General");
  await newTerminal(2);
  assert.equal(await where(2, "t1"), folder("strategy"), "terminal with Strategy active");

  await projectButton(/Beta/).click();
  await A.getByText(msg("projects.toast.active", { name: "Beta" })).first().waitFor();
  await oldChat("b1", folder("beta"));
  assert.equal(await newChat("n2"), home, "new chat with Beta active: General");
  await newTerminal(3);
  assert.equal(await where(3, "t2"), folder("beta"), "terminal with Beta active");

  await general().click();
  await A.getByText(msg("projects.toast.general")).first().waitFor();
  assert.equal(await general().getAttribute("aria-current"), "true");
  assert.equal((await server.api("/api/projects")).active_id, null, "General: no active project");
  await oldChat("g1", home);                         // the old chat came back to the root with General
  assert.equal(await newChat("n3"), home, "new chat in General");
  await newTerminal(4);
  assert.equal(await where(4, "t3"), home, "terminal in General: the root");

  // a new chat that picks Beta while General is chosen runs in Beta, and the panel stays in General
  await newLink(A).click();
  await scope(A).waitFor();
  await A.getByRole("radio", { name: "Beta" }).click();
  await send(A, "p1");
  await idle(A);
  assert.equal(cwds().at(-1), folder("beta"));
  assert.equal((await server.api("/api/projects")).active_id, null, "picking a project in a new chat does not leave General");

  // and a project can be chosen again afterwards
  await projectButton(/Strategy/).click();
  await A.getByText(msg("projects.toast.active", { name: "Strategy" })).first().waitFor();
  await oldChat("s2", folder("strategy"));
  assert.equal(await newChat("n4"), home);
  void B;
});
