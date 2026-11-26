import { screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useApiQuery } from "@/api";
import { setLocale, t } from "@/i18n";
import { failure, mockApi } from "@/screens/system/testkit";
import { renderWithApp } from "@/test/utils";
import { FiguresBlock } from "./FiguresBlock";
import { readFigures } from "./format";
import { SUMMARY } from "./testkit";
import type { RunSummary } from "./types";

beforeEach(() => setLocale("es"));
afterEach(() => vi.unstubAllGlobals());

function Harness({ run }: { run: number }) {
  return <FiguresBlock summary={useApiQuery<RunSummary>(`/api/verdict/runs/${run}/summary`)} />;
}

describe("Block C: figures (OPT-24)", () => {
  it("shows exactly what the server saved, with the regional format", async () => {
    mockApi({ "GET /api/verdict/runs/812/summary": SUMMARY });
    renderWithApp(<Harness run={812} />);
    await screen.findByText(t("verdict.figures.net"));
    const text = document.querySelector(".bt-figures")?.textContent ?? "";
    expect(text).toContain("18.420 USD");
    expect(text).toContain("18.950 USD");
    expect(text).toContain("1,31");
    expect(text).toContain("1,88");
    expect(text).toContain("0,92");
    expect(text).toContain("-9,4%");
    expect(text).toContain("74");
    expect(text).toContain("412");
    expect(text).toContain("54%");
    expect(text).toContain("1,41");
    expect(document.querySelector(".bt-costs")?.textContent).toContain([`${t("verdict.figures.commission")} 2060 USD`, `${t("verdict.figures.slippage")} 3090 USD`, `${t("verdict.figures.swap")} 0 USD`, `${t("verdict.figures.impact")} 0 USD`].join(" | "));
    expect(text).toContain(String(SUMMARY.ambiguous_bars));
  });

  it("uses the English format when the language is English", async () => {
    setLocale("en");
    mockApi({ "GET /api/verdict/runs/812/summary": SUMMARY });
    renderWithApp(<Harness run={812} />);
    await screen.findByText(t("verdict.figures.net"));
    expect(document.querySelector(".bt-figures")?.textContent).toContain("18,420 USD");
    expect(document.querySelector(".bt-figures")?.textContent).toContain("1.31");
  });

  it("does not make up a figure that was not saved: it says there is no data", () => {
    const figures = readFigures({ ...SUMMARY, stats: {} });
    expect(figures.sharpe).toBeNull();
    expect(figures.maxDrawdown).toBeNull();
    expect(figures.trades).toBeNull();
    expect(figures.net).toBe(18420);
  });

  it("says there are no saved figures when the server has none", async () => {
    mockApi({ "GET /api/verdict/runs/9/summary": () => failure(404, "no summary") });
    renderWithApp(<Harness run={9} />);
    await waitFor(() => expect(screen.getByText(t("verdict.figures.empty"))).toBeInTheDocument());
  });
});
