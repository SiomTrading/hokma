import { act, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { mockApi } from "@/screens/system/testkit";
import { renderWithApp } from "@/test/utils";
import { BacktestScreen, POLL_MS } from "./BacktestScreen";
import { resetOwnerGate, verdictApi } from "./api";
import { MONTECARLO, QUESTIONS, STRATEGIES, SUMMARY, THRESHOLDS_V4, WALKFORWARD, html, makeVerdict } from "./testkit";
import type { JobStatus } from "./types";

beforeEach(() => {
  setLocale("es");
  resetOwnerGate();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const PROPS = { tabKey: "backtest", params: { strategy: "ema_cross_nq", version: "3" }, active: true };

const job = (over: Partial<JobStatus>): JobStatus => ({
  job_id: 9,
  kind: "verdict",
  strategy_id: "ema_cross_nq",
  version: 3,
  state: "running",
  done: 0,
  total: 0,
  verdict_id: null,
  error: null,
  ...over,
});

function routes(extra: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    "GET /api/verdict/strategies": STRATEGIES,
    "GET /api/verdict/ema_cross_nq/3": makeVerdict(),
    "GET /api/verdict/thresholds/DT": THRESHOLDS_V4,
    "GET /api/verdict/runs/812/summary": SUMMARY,
    "GET /api/verdict/runs/812/report": () => html("<html><body>report</body></html>"),
    "GET /api/verdict/runs/820/walkforward": WALKFORWARD,
    "GET /api/verdict/runs/822/montecarlo": MONTECARLO,
    ...extra,
  };
}

describe("An evaluation that fails or has not counted its neighbors yet", () => {
  it("a failed job stops the wait and shows the classified error in words", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockApi(
      routes({
        "POST /api/verdict/ema_cross_nq/3/evaluate": { job_id: 9, state: "queued" },
        "GET /api/verdict/jobs/9": job({ state: "failed", error: { code: "data_changed", message: "data_changed: the bars changed" } }),
      }),
    );
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderWithApp(<BacktestScreen {...PROPS} />);
    await screen.findByText(t("verdict.variant.no_go"));
    await user.click(screen.getByRole("button", { name: t("verdict.evaluate") }));
    await screen.findByText(t("verdict.screen.evaluating"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS + 100);
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(t("verdict.error.code.data_changed"));
    expect(screen.queryByText(t("verdict.screen.evaluating"))).toBeNull();
    expect(screen.getByRole("button", { name: t("verdict.evaluate") })).toBeEnabled();
  });

  it("an error code the screen does not know falls back to the generic sentence", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockApi(
      routes({
        "POST /api/verdict/ema_cross_nq/3/evaluate": { job_id: 9, state: "queued" },
        "GET /api/verdict/jobs/9": job({ state: "failed", error: { code: "something_new", message: "something_new: x" } }),
      }),
    );
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderWithApp(<BacktestScreen {...PROPS} />);
    await screen.findByText(t("verdict.variant.no_go"));
    await user.click(screen.getByRole("button", { name: t("verdict.evaluate") }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS + 100);
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(t("verdict.error.generic"));
  });

  it("with total 0 it says it is preparing the neighbors, never 0 of 0, and then shows the count", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let current = job({ state: "running", done: 0, total: 0 });
    mockApi(
      routes({
        "POST /api/verdict/ema_cross_nq/3/evaluate": { job_id: 9, state: "queued" },
        "GET /api/verdict/jobs/9": () => current,
      }),
    );
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderWithApp(<BacktestScreen {...PROPS} />);
    await screen.findByText(t("verdict.variant.no_go"));
    await user.click(screen.getByRole("button", { name: t("verdict.evaluate") }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS + 100);
    });
    expect(await screen.findByText(t("verdict.screen.preparing"))).toBeInTheDocument();
    expect(screen.queryByText(t("verdict.screen.progress", { k: 0, n: 0 }))).toBeNull();
    expect(screen.queryByRole("progressbar")).toBeNull();
    current = job({ state: "running", done: 2, total: 8 });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS + 100);
    });
    expect(await screen.findByText(t("verdict.screen.progress", { k: 2, n: 8 }))).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("value", "2");
  });

  it("a done job brings the new verdict in", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let step = 0;
    mockApi(
      routes({
        "GET /api/verdict/ema_cross_nq/3": () => (step === 0 ? makeVerdict() : makeVerdict({ verdict_id: 77, verdict: "go" })),
        "POST /api/verdict/ema_cross_nq/3/evaluate": () => {
          step = 1;
          return { job_id: 9, state: "queued" };
        },
        "GET /api/verdict/jobs/9": job({ state: "done", done: 8, total: 8, verdict_id: 77 }),
      }),
    );
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderWithApp(<BacktestScreen {...PROPS} />);
    await screen.findByText(t("verdict.variant.no_go"));
    await user.click(screen.getByRole("button", { name: t("verdict.evaluate") }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS + 100);
    });
    await screen.findByText(t("verdict.variant.go"));
    expect(screen.queryByText(t("verdict.screen.evaluating"))).toBeNull();
  });
});

describe("A recompute that takes longer than 30 seconds", () => {
  it("the request is made with no timeout, so the client never cuts it short", async () => {
    vi.useFakeTimers();
    let signal: AbortSignal | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        signal = init?.signal ?? undefined;
        await new Promise((resolve) => setTimeout(resolve, 90_000));
        return new Response(JSON.stringify(makeVerdict()), { status: 200, headers: { "Content-Type": "application/json" } });
      }),
    );
    const pending = verdictApi.recompute(41, "thresholds");
    await vi.advanceTimersByTimeAsync(31_000);
    expect(signal?.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(60_000);
    await expect(pending).resolves.toMatchObject({ verdict_id: 41 });
  });

  it("the sheet of questions shows that it is recomputing while the server works, then the new verdict", async () => {
    let release: (() => void) | undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    mockApi(
      routes({
        "GET /api/verdict/ema_cross_nq/3/questions": QUESTIONS,
        "POST /api/verdict/ema_cross_nq/3/answers": { saved: 1 },
        "POST /api/verdict/41/recompute": async () => {
          await gate;
          return makeVerdict({ verdict: "review", verdict_id: 42 });
        },
      }),
    );
    renderWithApp(<BacktestScreen {...PROPS} />);
    await screen.findByText(t("verdict.variant.no_go"));
    await userEvent.click(screen.getAllByRole("button", { name: t("verdict.questions.answer") })[0] as HTMLElement);
    await userEvent.click(await screen.findByRole("button", { name: t("verdict.q.confirm") }));
    expect(await screen.findByText(t("verdict.recomputing"))).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    release?.();
    await screen.findByText(t("verdict.variant.review"));
    expect(screen.queryByText(t("verdict.recomputing"))).toBeNull();
  });
});

