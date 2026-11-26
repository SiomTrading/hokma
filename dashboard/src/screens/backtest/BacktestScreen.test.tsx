import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { resetRegistry } from "@/shell/registry";
import { BUILTIN_SCREENS, SIDEBAR_SCREENS, screenMeta } from "@/shell/screens";
import { mockApi } from "@/screens/system/testkit";
import type { Call } from "@/screens/system/testkit";
import { renderShell, renderWithApp } from "@/test/utils";
import { BacktestScreen, POLL_MS } from "./BacktestScreen";
import { resetOwnerGate } from "./api";
import { HEATMAP, MONTECARLO, QUESTIONS, STRATEGIES, SUMMARY, THRESHOLDS_V4, WALKFORWARD, fail, html, json, makeChecks, makeVerdict } from "./testkit";

beforeEach(() => {
  setLocale("es");
  resetOwnerGate();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const PROPS = { tabKey: "backtest", params: {}, active: true };

/** The routes of a strategy with every run of the verdict 41 saved. */
function routes(extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    "GET /api/verdict/strategies": STRATEGIES,
    "GET /api/verdict/ema_cross_nq/3": makeVerdict(),
    "GET /api/verdict/thresholds/DT": THRESHOLDS_V4,
    "GET /api/verdict/runs/812/summary": SUMMARY,
    "GET /api/verdict/runs/812/report": () => html("<html><body>report</body></html>"),
    "GET /api/verdict/runs/820/walkforward": WALKFORWARD,
    "GET /api/verdict/runs/822/montecarlo": MONTECARLO,
    "GET /api/verdict/optimizations/818/heatmap": HEATMAP,
    ...extra,
  };
}

const BLOCKS = ["verdict", "checks", "figures", "report", "optimization", "walkforward", "montecarlo", "suggestions", "notices"];
const blocks = () => Array.from(document.querySelectorAll("[data-block]")).map((el) => (el as HTMLElement).dataset.block);

describe("Registration and navigation (OPT-21)", () => {
  it("the Backtest entry sits right under Money desks in the side menu, with its own icon", () => {
    expect(SIDEBAR_SCREENS.indexOf("backtest")).toBe(SIDEBAR_SCREENS.indexOf("desks") + 1);
    expect(BUILTIN_SCREENS.backtest).toMatchObject({ titleKey: "screen.backtest", navKey: "nav.backtest", icon: "backtest" });
    expect(screenMeta("backtest")?.icon).toBe("backtest");
    expect(t("nav.backtest")).toBe("Backtest");
  });

  it("the side menu shows it after Money desks", () => {
    resetRegistry();
    renderShell();
    const menu = screen.getByRole("complementary", { name: t("nav.menu") });
    const names = within(menu).getAllByRole("button").map((b) => b.getAttribute("aria-label") ?? b.textContent ?? "");
    const desks = names.findIndex((n) => n.includes(t("nav.desks")));
    expect(desks).toBeGreaterThanOrEqual(0);
    expect(names[desks + 1]).toContain(t("nav.backtest"));
  });

  it("registers the screen with a title that names the strategy and version", async () => {
    vi.resetModules();
    const registry = await import("@/shell/registry");
    await import("./register");
    const registration = registry.getScreen("backtest");
    expect(registration?.component).toBeTypeOf("function");
    expect(registration?.title?.({}, t)).toBe("Backtest");
    expect(registration?.title?.({ strategy: "ema_cross_nq", version: "3" }, t)).toBe("Backtest: ema_cross_nq v3");
  });

  it("without parameters it shows the selector with the strategies that have a finished run", async () => {
    mockApi(routes());
    renderWithApp(<BacktestScreen {...PROPS} />);
    const select = await screen.findByRole("combobox", { name: t("verdict.select.label") });
    await waitFor(() => expect(within(select).getAllByRole("option")).toHaveLength(1 + STRATEGIES.length));
    expect(within(select).getByRole("option", { name: "ema_cross_nq v3 (DT)" })).toBeInTheDocument();
    expect(screen.getByText(t("verdict.screen.pick"))).toBeInTheDocument();
    expect(blocks()).toEqual([]);
  });

  it("choosing a strategy in the selector loads its verdict and the nine blocks in order", async () => {
    mockApi(routes());
    renderWithApp(<BacktestScreen {...PROPS} />);
    const select = await screen.findByRole("combobox", { name: t("verdict.select.label") });
    await waitFor(() => expect(within(select).getAllByRole("option").length).toBeGreaterThan(1));
    await userEvent.selectOptions(select, "ema_cross_nq@3");
    await screen.findByText(t("verdict.variant.no_go"));
    expect(blocks()).toEqual(BLOCKS);
  });

  it("opens straight on {strategy, version} from the tab parameters", async () => {
    const calls = mockApi(routes());
    renderWithApp(<BacktestScreen {...PROPS} params={{ strategy: "ema_cross_nq", version: "3" }} />);
    await screen.findByText(t("verdict.variant.no_go"));
    expect(calls.some((c) => c.path === "/api/verdict/ema_cross_nq/3")).toBe(true);
    expect(screen.getByRole("combobox", { name: t("verdict.select.label") })).toHaveValue("ema_cross_nq@3");
    expect(blocks()).toEqual(BLOCKS);
  });

  it("says there are no strategies with a run when the list is empty", async () => {
    mockApi({ "GET /api/verdict/strategies": [] });
    renderWithApp(<BacktestScreen {...PROPS} />);
    expect(await screen.findByText(t("verdict.screen.noStrategies"))).toBeInTheDocument();
  });
});

describe("Header and blocks together", () => {
  it("shows the last evaluation, the run, the thresholds and how many checks were removed", async () => {
    const verdict = makeVerdict({
      excluded: [{ key: "holdout_untouched", reason: null, by: "owner", at: "2026-11-25T10:00:00Z" }],
      checks: makeChecks({ holdout_untouched: { status: "unknown", excluded: true } }),
    });
    mockApi(routes({ "GET /api/verdict/ema_cross_nq/3": verdict }));
    renderWithApp(<BacktestScreen {...PROPS} params={{ strategy: "ema_cross_nq", version: "3" }} />);
    await screen.findByText(t("verdict.variant.no_go"));
    const meta = document.querySelector(".bt-meta")?.textContent ?? "";
    expect(meta).toContain(t("verdict.header.last", { when: "" }).trim());
    expect(meta).toContain(t("verdict.header.run", { run: 812 }));
    expect(meta).toContain(t("verdict.header.thresholds", { line: "DT", version: 4 }));
    expect(meta).toContain(t("verdict.header.removed.one"));
    expect(screen.getByText(t("verdict.header.inherited.one"))).toBeInTheDocument();
  });

  it("the Thresholds button of the line opens and closes the editor", async () => {
    mockApi(routes());
    renderWithApp(<BacktestScreen {...PROPS} params={{ strategy: "ema_cross_nq", version: "3" }} />);
    await screen.findByText(t("verdict.variant.no_go"));
    const button = screen.getByRole("button", { name: t("verdict.thresholds.open", { line: "DT" }) });
    await userEvent.click(button);
    expect(await screen.findByRole("region", { name: t("verdict.th.title", { line: "DT", version: 4 }) })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: t("verdict.th.close") }));
    expect(screen.queryByRole("region", { name: t("verdict.th.title", { line: "DT", version: 4 }) })).toBeNull();
  });

  it("Remove recalculates: the screen shows the verdict that comes back, the row struck and the count in the header", async () => {
    const after = makeVerdict({
      verdict: "needs_evidence",
      sentences: ["Suggestion: data is missing.", "Most solid: the deflated Sharpe.", "Suggested next step: answer."],
      excluded: [{ key: "pbo", reason: null, by: "owner", at: "2026-11-25T21:00:00Z" }],
      checks: makeChecks({ pbo: { status: "fail", excluded: true } }),
    });
    const calls = mockApi(routes({ "POST /api/verdict/41/exclude": after }));
    renderWithApp(<BacktestScreen {...PROPS} params={{ strategy: "ema_cross_nq", version: "3" }} />);
    await screen.findByText(t("verdict.variant.no_go"));
    await userEvent.click(screen.getByRole("button", { name: new RegExp(`^${t("verdict.row.exclude")}: ${t("verdict.check.pbo")}`) }));
    await userEvent.click(screen.getByRole("button", { name: t("verdict.row.confirmExclude") }));
    await screen.findByText(t("verdict.variant.needs_evidence"));
    expect(calls.filter((c) => c.method === "POST").map((c) => c.path)).toEqual(["/api/verdict/41/exclude"]);
    expect(document.querySelector('[data-check="pbo"]')).toHaveAttribute("data-status", "excluded");
    expect(document.querySelector(".bt-meta")?.textContent).toContain(t("verdict.header.removed.one"));
    expect(screen.getByText("Suggestion: data is missing.")).toBeInTheDocument();
  });

  it("answering from the sheet updates the suggestion in the same view", async () => {
    const after = makeVerdict({ verdict: "review", open_questions: [], sentences: ["Suggestion: review.", "Most solid.", "Next step."] });
    let questions = QUESTIONS;
    const calls = mockApi(
      routes({
        "GET /api/verdict/ema_cross_nq/3/questions": () => questions,
        "POST /api/verdict/ema_cross_nq/3/answers": () => {
          questions = questions.map((q) => ({ ...q, status: "answered" as const, answer: "yes" as const }));
          return { saved: 1 };
        },
        "POST /api/verdict/41/recompute": after,
      }),
    );
    renderWithApp(<BacktestScreen {...PROPS} params={{ strategy: "ema_cross_nq", version: "3" }} />);
    await screen.findByText(t("verdict.variant.no_go"));
    await userEvent.click(screen.getAllByRole("button", { name: t("verdict.questions.answer") })[0] as HTMLElement);
    await screen.findByRole("region", { name: t("verdict.q.title", { n: 3 }) });
    await userEvent.click(await screen.findByRole("button", { name: t("verdict.q.confirm") }));
    await screen.findByText(t("verdict.variant.review"));
    expect(screen.getByText("Suggestion: review.")).toBeInTheDocument();
    expect(calls.filter((c) => c.method === "POST").map((c) => c.path)).toEqual(["/api/verdict/ema_cross_nq/3/answers", "/api/verdict/41/recompute"]);
  });
});

describe("States of the screen (7.3)", () => {
  it("a version with no run says so", async () => {
    mockApi(routes({ "GET /api/verdict/ema_cross_nq/3": () => fail(404, "no_run", "no finished run") }));
    renderWithApp(<BacktestScreen {...PROPS} params={{ strategy: "ema_cross_nq", version: "3" }} />);
    expect(await screen.findByText(t("verdict.screen.noRun"))).toBeInTheDocument();
    expect(blocks()).toEqual([]);
    expect(screen.getByRole("button", { name: t("verdict.evaluate") })).toBeDisabled();
  });

  it("a run with no walk-forward says it in A and leaves E and F empty with their reason", async () => {
    const verdict = makeVerdict({
      verdict: "needs_evidence",
      runs: { run: 812, walkforward: null, optimize: null, montecarlo: null },
      ideal: { status: "not_found", params: null, plateau_size: null, run_id: null, reason: "no_optimization" },
    });
    mockApi(routes({ "GET /api/verdict/ema_cross_nq/3": verdict }));
    renderWithApp(<BacktestScreen {...PROPS} params={{ strategy: "ema_cross_nq", version: "3" }} />);
    await screen.findByText(t("verdict.variant.needs_evidence"));
    expect(screen.getByText(t("verdict.screen.noWalkForward"))).toBeInTheDocument();
    expect(blocks()).toEqual(BLOCKS);
    expect(within(document.querySelector('[data-block="walkforward"]') as HTMLElement).getByText(t("verdict.wf.empty"))).toBeInTheDocument();
    expect(within(document.querySelector('[data-block="optimization"]') as HTMLElement).getByText(t("verdict.opt.empty"))).toBeInTheDocument();
    expect(within(document.querySelector('[data-block="montecarlo"]') as HTMLElement).getByRole("button", { name: t("verdict.mc.run") })).toBeInTheDocument();
    expect(within(document.querySelector('[data-block="notices"]') as HTMLElement).getByText(t("verdict.notice.monte_carlo_missing"))).toBeInTheDocument();
  });

  it("a version that was never evaluated offers to evaluate and, when the server finishes, shows the verdict", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let step = 0;
    const calls = mockApi(
      routes({
        "GET /api/verdict/ema_cross_nq/3": () => {
          if (step === 0) return fail(404, "no_verdict", "none");
          if (step === 1) return fail(404, "no_verdict", "none");
          return makeVerdict({ verdict_id: 50 });
        },
        "POST /api/verdict/ema_cross_nq/3/evaluate": () => {
          step = 1;
          return { job_id: 9 };
        },
      }),
    );
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderWithApp(<BacktestScreen {...PROPS} params={{ strategy: "ema_cross_nq", version: "3" }} />);
    expect(await screen.findByText(t("verdict.screen.noVerdict"))).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: t("verdict.evaluate.first") }));
    await screen.findByText(t("verdict.screen.evaluating"));
    expect(calls.filter((c) => c.method === "POST").map((c) => c.path)).toEqual(["/api/verdict/ema_cross_nq/3/evaluate"]);
    step = 2;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS + 100);
    });
    await screen.findByText(t("verdict.variant.no_go"));
    expect(screen.queryByText(t("verdict.screen.evaluating"))).toBeNull();
  });

  it("while it evaluates it shows the progress of the neighbors and keeps the previous evaluation in view", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let phase = 0;
    mockApi(
      routes({
        "GET /api/verdict/ema_cross_nq/3": () => (phase === 0 ? makeVerdict() : makeVerdict({ evaluating: { done: 3, total: 12 } })),
        "POST /api/verdict/ema_cross_nq/3/evaluate": () => {
          phase = 1;
          return { job_id: 9 };
        },
      }),
    );
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderWithApp(<BacktestScreen {...PROPS} params={{ strategy: "ema_cross_nq", version: "3" }} />);
    await screen.findByText(t("verdict.variant.no_go"));
    await user.click(screen.getByRole("button", { name: t("verdict.evaluate") }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS + 100);
    });
    expect(await screen.findByText(t("verdict.screen.progress", { k: 3, n: 12 }))).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("value", "3");
    expect(screen.getByText(t("verdict.variant.no_go"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("verdict.evaluate") })).toBeDisabled();
  });

  it("a line with no thresholds shows the notice and creates them copying SWC", async () => {
    const calls: Call[] = mockApi({
      "GET /api/verdict/strategies": STRATEGIES,
      "GET /api/verdict/lp_core/2": () => fail(404, "no_verdict", "none"),
      "GET /api/verdict/thresholds/LP": () => json({ code: "bad_input", reason: "no_thresholds", detail: "no thresholds" }, 400),
      "POST /api/verdict/thresholds/LP/copy-from/SWC": { ...THRESHOLDS_V4, line: "LP", version: 1 },
    });
    renderWithApp(<BacktestScreen {...PROPS} params={{ strategy: "lp_core", version: "2" }} />);
    expect(await screen.findByText(t("verdict.screen.noThresholds", { line: "LP" }))).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: t("verdict.screen.copyThresholds") }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path.endsWith("/copy-from/SWC"))).toBe(true));
  });

  it("without the owner confirmation everything is visible but Remove, Save and Answer are off, with the notice", async () => {
    mockApi(
      routes({
        "POST /api/verdict/41/exclude": () => json({ code: "owner_confirmation_required", detail: "owner confirmation required" }, 403),
      }),
    );
    renderWithApp(<BacktestScreen {...PROPS} params={{ strategy: "ema_cross_nq", version: "3" }} />);
    await screen.findByText(t("verdict.variant.no_go"));
    expect(screen.queryByText(t("verdict.owner.missing"))).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: new RegExp(`^${t("verdict.row.exclude")}: ${t("verdict.check.pbo")}`) }));
    await userEvent.click(screen.getByRole("button", { name: t("verdict.row.confirmExclude") }));
    expect(await screen.findByText(t("verdict.owner.missing"))).toBeInTheDocument();
    for (const button of screen.getAllByRole("button", { name: new RegExp(`^${t("verdict.row.exclude")}: `) })) expect(button).toBeDisabled();
    for (const button of screen.getAllByRole("button", { name: t("verdict.questions.answer") })) expect(button).toBeDisabled();
    // the screen is still readable
    expect(blocks()).toEqual(BLOCKS);
    await userEvent.click(screen.getByRole("button", { name: t("verdict.thresholds.open", { line: "DT" }) }));
    expect(await screen.findByRole("button", { name: t("verdict.th.save") })).toBeDisabled();
  });
});

describe("The header counts what the owner took out, not what does not apply", () => {
  it("a verdict whose only exclusion is the system's says nothing about removed checks", async () => {
    mockApi(
      routes({
        "GET /api/verdict/ema_cross_nq/3": makeVerdict({
          checks: makeChecks({ survivorship_free: { status: "unknown", excluded: true } }),
          excluded: [{ key: "survivorship_free", reason: "not_applicable:single_instrument", by: "system", at: "2026-11-25T10:00:00Z" }],
        }),
      }),
    );
    renderWithApp(<BacktestScreen {...PROPS} params={{ strategy: "ema_cross_nq", version: "3" }} />);
    await screen.findByText(t("verdict.variant.no_go"));
    expect(document.querySelector(".bt-meta")?.textContent).not.toContain(t("verdict.header.removed.one"));
    expect(document.querySelector(".bt-inherited")).toBeNull();
  });
});
