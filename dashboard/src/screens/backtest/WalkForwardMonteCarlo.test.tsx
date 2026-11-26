import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useApiQuery } from "@/api";
import { setLocale, t } from "@/i18n";
import { mockApi } from "@/screens/system/testkit";
import { renderWithApp } from "@/test/utils";
import { MonteCarloBlock } from "./MonteCarloBlock";
import { WalkForwardBlock } from "./WalkForwardBlock";
import { resetOwnerGate } from "./api";
import { MONTECARLO, WALKFORWARD } from "./testkit";
import type { MonteCarloData, WalkForwardData } from "./types";

beforeEach(() => {
  setLocale("es");
  resetOwnerGate();
});
afterEach(() => vi.unstubAllGlobals());

function Walk({ id }: { id: number | null }) {
  const query = useApiQuery<WalkForwardData>(id ? `/api/verdict/runs/${id}/walkforward` : null);
  return <WalkForwardBlock query={id ? query : null} />;
}

function Carlo({ id, ownerBlocked = false }: { id: number | null; ownerBlocked?: boolean }) {
  const query = useApiQuery<MonteCarloData>(id ? `/api/verdict/runs/${id}/montecarlo` : null);
  return <MonteCarloBlock runId={812} query={id ? query : null} ownerBlocked={ownerBlocked} />;
}

describe("Block F: walk-forward (OPT-27)", () => {
  it("shows the summary line and one row per window with what the server saved", async () => {
    mockApi({ "GET /api/verdict/runs/820/walkforward": WALKFORWARD });
    renderWithApp(<Walk id={820} />);
    const table = await screen.findByRole("table", { name: t("verdict.wf.caption") });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(1 + WALKFORWARD.windows.length);
    const first = within(rows[1] as HTMLElement).getAllByRole("cell");
    expect(first.map((c) => c.textContent)).toEqual([expect.stringMatching(/2019.*2020/), expect.stringMatching(/2021/), "fast 12, slow 150", "3100,00", "1200,00"]);
    // a window with no dates shows the row numbers of the server
    const second = within(rows[2] as HTMLElement).getAllByRole("cell");
    expect(second[0]?.textContent).toBe(t("verdict.wf.rows", { from: "300", to: "1500" }));
    expect(second[3]?.textContent).toBe("2900,50");
    const summary = document.querySelector(".bt-wf-summary")?.textContent ?? "";
    expect(summary).toContain(t("verdict.wf.summary", { scheme: t("verdict.wf.scheme.rolling"), n: 2 }));
    expect(summary).toContain("0,62");
    expect(summary).toContain("7300,00");
    expect(summary).toContain(t("verdict.wf.noLeak"));
  });

  it("says when there is a leak", async () => {
    mockApi({ "GET /api/verdict/runs/820/walkforward": { ...WALKFORWARD, leakage_free: false } });
    renderWithApp(<Walk id={820} />);
    await screen.findByRole("table");
    expect(screen.getByText(new RegExp(t("verdict.wf.leak")))).toBeInTheDocument();
  });

  it("is empty with its reason when the run has no walk-forward", () => {
    renderWithApp(<Walk id={null} />);
    expect(screen.getByText(t("verdict.wf.empty"))).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });
});

describe("Block G: Monte Carlo (OPT-27)", () => {
  it("shows the method, the spread of each figure and the placebo share", async () => {
    mockApi({ "GET /api/verdict/runs/822/montecarlo": MONTECARLO });
    renderWithApp(<Carlo id={822} />);
    await screen.findByText(t("verdict.mc.method", { method: "bootstrap", n: "1000", seed: 7 }));
    const list = document.querySelector(".bt-mc")?.textContent ?? "";
    expect(list).toContain("Sharpe: peor 5% 0,41 | mediana 1,22 | mejor 5% 2,03");
    expect(list).toContain("Rendimiento anual");
    expect(screen.getByText(t("verdict.mc.placebo", { pct: "3%", p: "0,03" }))).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: t("verdict.mc.run") })).toBeNull();
  });

  it("when it is missing it offers Run Monte Carlo, posts it and shows the result", async () => {
    const calls = mockApi({ "POST /api/verdict/runs/812/montecarlo": MONTECARLO });
    renderWithApp(<Carlo id={null} />);
    expect(screen.getByText(t("verdict.mc.missing"))).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: t("verdict.mc.run") }));
    await screen.findByText(t("verdict.mc.method", { method: "bootstrap", n: "1000", seed: 7 }));
    expect(calls.map((c) => `${c.method} ${c.path}`)).toEqual(["POST /api/verdict/runs/812/montecarlo"]);
    expect(screen.getByText(t("verdict.mc.done"))).toBeInTheDocument();
  });

  it("when the saved one is not found it also offers the button", async () => {
    mockApi({});
    renderWithApp(<Carlo id={822} />);
    await waitFor(() => expect(screen.getByRole("button", { name: t("verdict.mc.run") })).toBeInTheDocument());
  });

  it("the button is disabled without the owner confirmation", () => {
    renderWithApp(<Carlo id={null} ownerBlocked />);
    expect(screen.getByRole("button", { name: t("verdict.mc.run") })).toBeDisabled();
  });

  it("shows the error of a failed run and keeps the button", async () => {
    mockApi({ "POST /api/verdict/runs/812/montecarlo": () => new Response(JSON.stringify({ code: "bad_run", detail: "run is not finished" }), { status: 400, headers: { "Content-Type": "application/json" } }) });
    renderWithApp(<Carlo id={null} />);
    await userEvent.click(screen.getByRole("button", { name: t("verdict.mc.run") }));
    expect(await screen.findByRole("alert")).toHaveTextContent("run is not finished");
    expect(screen.getByRole("button", { name: t("verdict.mc.run") })).toBeEnabled();
  });
});
