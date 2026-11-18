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

  // 1. menu, Money desks, Create desk, From a template, Standard, name, capital, asset, budget
  await page.getByRole("button", { name: msg("nav.desks") }).first().click();
  await page.getByText(msg("desks.fund.empty.title")).waitFor();
  await page.getByRole("button", { name: msg("desks.create.button"), exact: true }).click();
  await page.getByRole("dialog", { name: msg("desks.start.title") }).getByRole("button", { name: new RegExp("^" + msg("desks.start.template.name")) }).click();
  const wizard = page.getByRole("dialog", { name: msg("desks.wizard.title") });
  await wizard.getByLabel(msg("desks.field.name")).fill("Futuros NQ");
  await wizard.getByLabel(msg("desks.field.capital")).fill("10000");
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
  const row = page.getByRole("button", { name: msg("desks.agents.select", { name: signalsBefore.name }) });
  await row.click();
  const models = page.getByRole("group", { name: msg("desks.agents.model") });
  await models.getByRole("button", { name: msg("desks.model.opus"), exact: true }).click();
  await page.getByRole("button", { name: msg("agents.opus.confirm"), exact: true }).click();
  await until(async () => (await agentsOf(desk.id)).find((a) => a.agent_slug === "signals").model === "opus", "signals on Opus");
  await reloadWithHandoff(page);
  await tab("agents").click();
  await page.getByRole("button", { name: msg("desks.agents.select", { name: signalsBefore.name }) }).click();
  assert.equal(await page.getByRole("group", { name: msg("desks.agents.model") }).getByRole("button", { name: msg("desks.model.opus"), exact: true }).getAttribute("aria-pressed"), "true");
  const otherTeam = await agentsOf(otherId);
  assert.ok(!otherTeam.some((a) => a.agent_slug === "signals" && a.model === "opus"), "the other desk is untouched");

  // 3. Terminal: asset list, chart, then write to the chief and see the tokens in this desk only
  await tab("terminal").click();
  await page.getByRole("region", { name: msg("desks.terminal.list") }).or(page.getByLabel(msg("desks.terminal.list"))).first().waitFor();
  await page.getByText("NQ=F").first().waitFor();
  await page.getByLabel(msg("desks.terminal.chart", { symbol: "NQ=F" })).first().waitFor();
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
});
