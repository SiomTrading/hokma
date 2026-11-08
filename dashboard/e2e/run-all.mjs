/**
 * One command for every E2E: `npm run e2e` (all suites, es and en) or `npm run e2e -- projects terminal` (some).
 * Each suite starts and stops its own backend on a free port with temporary data. The dashboard is built once and
 * shared. Exit code is non-zero when any suite fails in any language.
 */
import { spawn } from "node:child_process";
import path from "node:path";
import { buildDashboard, here } from "./lib.mjs";

const SUITES = ["chat", "agents", "chart", "chart-state", "first-run", "projects", "terminal", "update", "permissions", "connections", "session", "resume", "shutdown"];
const wanted = process.argv.slice(2);
const unknown = wanted.filter((w) => !SUITES.includes(w));
if (unknown.length) {
  console.error(`unknown suite: ${unknown.join(", ")}. Available: ${SUITES.join(", ")}`);
  process.exit(2);
}
const suites = wanted.length ? wanted : SUITES;

const dist = buildDashboard();
const results = [];
for (const suite of suites) {
  const started = Date.now();
  const output = [];
  const code = await new Promise((resolve) => {
    const child = spawn("node", [path.join(here, `${suite}.e2e.mjs`)], { env: { ...process.env, E2E_DIST: dist }, stdio: ["ignore", "pipe", "pipe"] });
    const keep = (d) => {
      process.stdout.write(d);
      output.push(String(d));
    };
    child.stdout.on("data", keep);
    child.stderr.on("data", keep);
    child.on("close", (c) => resolve(c ?? 1));
  });
  const text = output.join("");
  const locales = ["es", "en"].map((l) => `${l}:${new RegExp(`^ok (?:${suite} )?${l}$`, "m").test(text) ? "ok" : "FAIL"}`);
  results.push({ suite, code, locales, seconds: Math.round((Date.now() - started) / 1000) });
}

console.log("\nE2E summary");
for (const r of results) console.log(`  ${r.suite.padEnd(10)} ${r.locales.join("  ")}  ${r.seconds}s${r.code === 0 ? "" : "  FAILED"}`);
const failed = results.filter((r) => r.code !== 0 || r.locales.some((l) => l.endsWith("FAIL")));
console.log(failed.length ? `${failed.length} suite(s) failed` : `all ${results.length} suites passed in es and en`);
process.exit(failed.length ? 1 : 0);
