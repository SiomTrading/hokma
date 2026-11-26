/**
 * Money desks E2E (SPEC-014, P13): the real backend (e2e/desks-serve.py), the real compiled dashboard, a real Chromium
 * and the fake `claude` of backend/tests/engine_fixtures on PATH. Create a desk from the Standard template, give one
 * agent another model, talk to the desk chief and watch the tokens land in that desk only, then cross the 80% and 100%
 * lines of the weekly budget and raise it again. No UI text lives here: every label comes from the message files.
 *
 *   node e2e/desks.e2e.mjs [es|en]        (both locales when omitted)
 */
import assert from "node:assert/strict";
import { reloadWithHandoff, runSuite, until } from "./lib.mjs";

await runSuite("desks", { script: "desks-serve.py", signedIn: true }, async ({ page, server, msg }) => {
  const desks = async () => (await server.api("/api/desks")).desks ?? (await server.api("/api/desks"));
  const agentsOf = async (id) => {
    const body = await server.api(`/api/desks/${id}/agents`);
    return Array.isArray(body) ? body : body.agents;
  };
  const usageOf = (id) => server.api(`/api/desks/${id}/usage`);
  const tab = (key) => page.getByRole("tab", { name: msg(`desks.tab.${key}`), exact: true });
  await page.goto(server.base);

  // 1. menu, Money desks, Create desk, From a template, Standard, name, asset, budget (the capital comes from the accounts now)
  await page.getByRole("button", { name: msg("nav.desks") }).first().click();
  await page.getByText(msg("desks.fund.empty.title")).waitFor();
  await page.getByRole("button", { name: msg("desks.create.button"), exact: true }).click();
  await page.getByRole("dialog", { name: msg("desks.start.title") }).getByRole("button", { name: new RegExp("^" + msg("desks.start.template.name")) }).click();
  const wizard = page.getByRole("dialog", { name: msg("desks.wizard.title") });
  await wizard.getByLabel(msg("desks.field.name")).fill("Futuros NQ");
  await wizard.getByRole("button", { name: msg("desks.wizard.next.team") }).click();
  await wizard.getByRole("radio", { name: new RegExp(msg("desks.tpl.standard.name")) }).click();
  await wizard.getByRole("button", { name: msg("desks.wizard.next.rules") }).click();
  await wizard.getByRole("textbox", { name: msg("desks.budget.weekly") }).fill("1000000");
  await wizard.getByRole("button", { name: msg("desks.wizard.next.assets") }).click();
  await wizard.getByLabel(msg("desks.field.asset")).fill("NQ=F");
  await wizard.getByRole("button", { name: msg("desks.field.asset.add"), exact: true }).click();
  await wizard.getByRole("button", { name: msg("desks.wizard.create"), exact: true }).last().click();
  await wizard.waitFor({ state: "detached" });
  const [desk] = await until(async () => {
    const list = await desks();
    return list.length === 1 ? list : null;
  }, "the desk in the list");
  assert.equal(desk.name, "Futuros NQ");
  const team = await agentsOf(desk.id);
  assert.equal(team.length, 7, "the Standard template has 7 agents");
  assert.equal(team.filter((a) => a.role === "chief").length, 1);
  // it shows in the menu and in the Fund with its team
  await page.getByRole("button", { name: new RegExp(`^Futuros NQ, `) }).first().waitFor();
  await page.getByRole("button", { name: msg("nav.desks") }).first().click();          // creating opens the desk; the Fund shows its card
  await page.getByRole("button", { name: msg("desks.card.open", { name: "Futuros NQ" }) }).waitFor();

  // 2. a Minimal desk next to it, then Agents: signals goes to Opus here and stays Sonnet or Haiku there
  const minimal = await server.api("/api/desks", {
    method: "POST",
    body: JSON.stringify({ name: "Solo chief", focus: "x", capital: 5000, template: "minimal", assets: [] }),
  });
  const otherId = minimal.id ?? minimal.desk?.id;
  assert.ok(otherId, "the Minimal desk was created");
  const signalsBefore = (await agentsOf(desk.id)).find((a) => a.agent_slug === "signals");
  await page.getByRole("button", { name: msg("desks.card.open", { name: "Futuros NQ" }) }).click();
  await tab("agents").click();
  const row = page.getByRole("button", { name: msg("desks.agents.select", { name: msg("agents.kit.signals.name") }) });
  await row.click();
  const models = page.getByRole("group", { name: msg("desks.agents.model") });
  await models.getByRole("button", { name: msg("desks.model.opus"), exact: true }).click();
  await page.getByRole("button", { name: msg("agents.opus.confirm"), exact: true }).click();
  await until(async () => (await agentsOf(desk.id)).find((a) => a.agent_slug === "signals").model === "opus", "signals on Opus");
  await reloadWithHandoff(page);
  await tab("agents").click();
  await page.getByRole("button", { name: msg("desks.agents.select", { name: msg("agents.kit.signals.name") }) }).click();
  assert.equal(await page.getByRole("group", { name: msg("desks.agents.model") }).getByRole("button", { name: msg("desks.model.opus"), exact: true }).getAttribute("aria-pressed"), "true");
  const otherTeam = await agentsOf(otherId);
  assert.ok(!otherTeam.some((a) => a.agent_slug === "signals" && a.model === "opus"), "the other desk is untouched");

  // 3. Terminal: asset list, chart, then write to the chief and see the tokens in this desk only
  await tab("terminal").click();
  await page.getByRole("region", { name: msg("desks.terminal.list") }).or(page.getByLabel(msg("desks.terminal.list"))).first().waitFor();
  await page.getByText("NQ=F").first().waitFor();
  await page.getByLabel(msg("desks.terminal.chart", { symbol: "NQ=F" })).first().waitFor();
  // the chart has the same intervals as the Chart screen: weekly from the bar, then 3h and 1W written in the box
  const chartTitle = page.locator(".dterm-chart .dterm-tab");
  await page.getByRole("group", { name: msg("chart.timeframe") }).getByRole("button", { name: "1W", exact: true }).click();
  await until(async () => (await chartTitle.innerText()).includes("1W"), "weekly in the Terminal chart");
  for (const typed of ["3h", "1W"]) {
    await page.getByRole("button", { name: msg("chart.interval.more") }).click();
    await page.getByRole("combobox", { name: msg("chart.interval.label") }).fill(typed);
    await page.keyboard.press("Enter");
    await until(async () => (await chartTitle.innerText()).includes(typed), `${typed} typed in the Terminal chart`);
  }
  await page.getByRole("button", { name: msg("chart.interval.more") }).click();
  await page.getByRole("combobox", { name: msg("chart.interval.label") }).fill("zzz");
  await page.keyboard.press("Enter");
  await page.getByRole("alert").filter({ hasText: msg("chart.interval.invalid") }).waitFor();
  await page.keyboard.press("Escape");
  const input = page.getByLabel(msg("desks.chat.input"));
  await input.fill("Status of the desk?");
  await input.press("Enter");
  await until(async () => (await usageOf(desk.id)).used > 0, "tokens of the turn on the desk");
  const used = (await usageOf(desk.id)).used;
  assert.equal((await usageOf(otherId)).used, 0, "the other desk spent nothing");
  const weekly = await server.api("/api/usage/weekly");
  assert.ok(weekly, "Settings usage keeps answering");
  await tab("agents").click();
  const chief = (await agentsOf(desk.id)).find((a) => a.role === "chief");
  assert.ok(chief.tokens_week > 0, "the chief shows tokens of the week");
  await page.getByText(msg("desks.agents.budget"), { exact: true }).waitFor();

  // 4. budget: the cap goes to just above the spend (80% band), then to the spend itself (100%), then it is raised
  const setCap = async (value) => {
    await tab("config").click();
    const field = page.getByLabel(msg("desks.cfg.budget"));
    await field.fill(String(value));
    await page.getByRole("button", { name: msg("desks.cfg.save") }).click();
    await page.getByText(msg("desks.cfg.saved")).waitFor();
    await tab("agents").click();
  };
  await setCap(Math.ceil(used / 0.9));
  await page.getByText(msg("desks.agents.budget.warn")).waitFor();
  assert.equal((await usageOf(desk.id)).state, "warn");
  await setCap(used);
  await page.getByText(msg("desks.agents.budget.exhausted")).waitFor();
  assert.equal((await usageOf(desk.id)).state, "exhausted");
  await tab("terminal").click();
  await page.getByText(msg("desks.chat.budget.exhausted")).waitFor();
  assert.equal(await page.getByLabel(msg("desks.chat.input")).isDisabled(), true, "the desk chat is paused");
  assert.equal((await usageOf(otherId)).state, "ok");
  await setCap(used * 10);
  await tab("terminal").click();
  await until(async () => !(await page.getByLabel(msg("desks.chat.input")).isDisabled()), "the desk chat resumes");
  const again = page.getByLabel(msg("desks.chat.input"));
  await again.fill("And now?");
  await again.press("Enter");
  await until(async () => (await usageOf(desk.id)).used > used, "a second turn after raising the cap");
  assert.equal((await usageOf(otherId)).used, 0, "the other desk still spent nothing");

  // 5. a desk with no assets still has a working Terminal: list with its add button, invitation, chief chat, no step labels
  await page.getByLabel(msg("desks.bar.pick")).selectOption(String(otherId));
  await tab("terminal").click();
  await page.getByLabel(msg("desks.terminal.list")).getByRole("button", { name: msg("desks.terminal.add") }).waitFor();
  await page.getByText(msg("desks.terminal.noAssets.title")).waitFor();
  await until(async () => !(await page.getByLabel(msg("desks.chat.input")).isDisabled()), "the chat of the empty desk is ready");
  assert.equal(await page.getByText(/paso \d|step \d/i).count(), 0, "no step labels on screen");

  // 6. resizable chat, foldable Positions, assets in the watchlist only, and a Settings menu that always shows its text
  await page.getByLabel(msg("desks.bar.pick")).selectOption(String(desk.id));
  await tab("terminal").click();
  const grid = page.locator(".dterm");
  const chatWidth = async () => Math.round((await page.getByLabel(msg("desks.terminal.chat"), { exact: true }).first().boundingBox()).width);
  await page.getByText("NQ=F").first().waitFor();
  assert.equal(await chatWidth(), 380, "the chat starts at 380 px");
  const bar = page.getByRole("separator", { name: msg("desks.terminal.resize") });
  const box = await bar.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x - 80, box.y + box.height / 2, { steps: 5 });
  await page.mouse.up();
  assert.ok((await chatWidth()) > 440, "dragging the bar left widens the chat");
  const wide = await chatWidth();
  await page.reload();
  await page.getByText("NQ=F").first().waitFor();
  assert.equal(await chatWidth(), wide, "the desk remembers the width after a reload");
  const fold = page.getByRole("button", { name: msg("desks.terminal.positions.fold") });
  const chartBefore = (await page.getByLabel(msg("desks.terminal.chartEmpty")).or(page.locator(".dterm-chart")).first().boundingBox()).height;
  await fold.click();
  await page.getByText(msg("desks.terminal.positions.title")).waitFor({ state: "detached" });
  assert.ok((await page.locator(".dterm-chart").first().boundingBox()).height > chartBefore + 100, "folded Positions gives its height to the chart");
  await page.reload();
  await page.getByRole("button", { name: msg("desks.terminal.positions.unfold") }).waitFor();
  assert.ok(await grid.evaluate((el) => el.classList.contains("folded")), "the desk remembers Positions folded");
  await page.getByRole("button", { name: msg("desks.terminal.positions.unfold") }).click();
  await page.getByText(msg("desks.terminal.positions.title")).waitFor();
  await tab("config").click();
  const menu = page.getByRole("navigation", { name: msg("desks.cfg.nav") });
  for (const id of ["objective", "connections", "memory", "accounts"]) {
    await menu.getByRole("button", { name: new RegExp(msg(`desks.cfg.section.${id}`)) }).click();
    for (const other of ["objective", "connections", "memory", "accounts"]) {
      const label = menu.getByText(msg(`desks.cfg.section.${other}`), { exact: true });
      const area = await label.boundingBox();
      assert.ok(area && area.width > 20 && area.height > 8, `the menu text of ${other} is visible while ${id} is open`);
    }
  }
  for (const el of await menu.getByRole("button").all()) {
    const inside = await el.evaluate((b) => [...b.children].every((c) => c.getBoundingClientRect().right <= b.getBoundingClientRect().right + 1));
    assert.ok(inside, "nothing in a menu entry spills out of it");
  }
  assert.equal(await menu.getByRole("button").count(), 4, "Settings has no Assets section");
  await menu.getByText(msg("desks.cfg.sim"), { exact: true }).waitFor();
  await tab("terminal").click();
  const watch = page.getByLabel(msg("desks.terminal.list"));
  await watch.getByRole("button", { name: msg("desks.field.asset.remove", { symbol: "NQ=F" }) }).click();
  await watch.getByRole("button", { name: msg("desks.terminal.remove.yes") }).click();
  await page.getByText(msg("desks.terminal.noAssets.title")).waitFor();
  assert.deepEqual((await server.api(`/api/desks/${desk.id}`)).assets.map((a) => a.symbol ?? a), [], "the asset left the desk");
});
