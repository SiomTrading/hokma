/**
 * Terminal E2E: the dock against the real backend and a real shell on a real PTY. It starts by itself, runs commands,
 * opens a second terminal inside the active project, switches tabs, sees a process end and closes the tabs.
 *
 *   node e2e/terminal.e2e.mjs [es|en]        (both locales when omitted)
 */
import assert from "node:assert/strict";
import { runSuite, until } from "./lib.mjs";

await runSuite("terminal", { script: "serve.py" }, async ({ page, server, msg }) => {
  const dock = page.getByRole("region", { name: msg("terminal.title") });
  const screenOf = (n) => dock.getByRole("group", { name: msg("term.screen", { n }) });
  const rows = (n) => screenOf(n).locator(".xterm-rows");
  const text = async (n) => (await rows(n).innerText()).replace(/ /g, " ");
  const run = async (n, command) => {
    await screenOf(n).click();
    await page.keyboard.type(command);
    await page.keyboard.press("Enter");
  };
  await page.goto(server.base);

  // 1. the first terminal starts by itself, inside the ACTIVE project (never the bare working folder), and runs real commands
  await rows(1).waitFor();
  await until(async () => (await text(1)).trim().length > 0, "the shell prompt");
  await run(1, "echo hk-$((20+22))");
  await until(async () => (await text(1)).includes("hk-42"), "the command output (computed by the shell)");
  await run(1, "echo here=$(pwd)");
  await until(async () => (await text(1)).includes(`here=${server.home}/projects/principal`), "the active project folder as current directory of the FIRST terminal");

  // 2. switch the active project and open another terminal: it starts inside the newly active project
  await page.getByRole("complementary", { name: msg("rightPanel.title") }).getByRole("list", { name: msg("projects.list") }).getByRole("button", { name: /Strategy/ }).click();
  await page.getByText(msg("projects.toast.active", { name: "Strategy" })).first().waitFor();
  await dock.getByRole("button", { name: msg("terminal.new") }).click();
  const tabs = dock.getByRole("tablist", { name: msg("term.tabs") });
  await tabs.waitFor();
  await tabs.getByText(msg("term.tab", { n: 2 })).waitFor();
  await rows(2).waitFor();
  await until(async () => (await text(2)).trim().length > 0, "the second prompt");
  await run(2, "echo here=$(pwd)");
  await until(async () => (await text(2)).includes(`here=${server.home}/projects/strategy`), "the project folder as current directory");

  // 3. back to the first tab: its session and its output are intact
  await tabs.getByText(msg("term.tab", { n: 1 })).click();
  await rows(1).waitFor();
  await until(async () => (await text(1)).includes("hk-42"), "the first terminal kept its output");

  // 4. a process that ends is reported, not hidden
  await tabs.getByText(msg("term.tab", { n: 2 })).click();
  await run(2, "exit");
  await dock.getByText(msg("term.exited", { code: 0 })).waitFor({ timeout: 15000 });

  // 5. close the finished tab; the other keeps working
  await dock.getByRole("button", { name: msg("term.tabClose", { n: 2 }) }).click();
  await rows(1).waitFor();
  await run(1, "echo again=$((6*7))");
  await until(async () => (await text(1)).includes("again=42"), "the first terminal still answers");

  // 6. closing the last one leaves the empty state with the action to open a new one
  await dock.getByRole("button", { name: msg("term.tabClose", { n: 1 }) }).click();
  await dock.getByText(msg("term.empty.title")).waitFor();
  await dock.locator("button.btn", { hasText: msg("term.empty.action") }).click();
  await rows(3).waitFor();
});
