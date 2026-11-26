import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { mockApi } from "@/screens/system/testkit";
import { renderWithApp } from "@/test/utils";
import { OptimizationBlock, markerPosition } from "./OptimizationBlock";
import { svgDataUrl } from "./format";
import { HEATMAP, makeVerdict } from "./testkit";

beforeEach(() => setLocale("es"));
afterEach(() => vi.unstubAllGlobals());

const IDEAL = makeVerdict().ideal;

describe("Block E: heat map (OPT-26)", () => {
  it("shows the SVG as a data: image and never inserts it as HTML", async () => {
    mockApi({ "GET /api/verdict/optimizations/818/heatmap": HEATMAP });
    const { container } = renderWithApp(<OptimizationBlock optId={818} ideal={IDEAL} />);
    const img = (await screen.findByRole("img", { name: t("verdict.opt.alt", { metric: "sharpe", x: "fast", y: "slow" }) })) as HTMLImageElement;
    expect(img.getAttribute("src")).toMatch(/^data:image\/svg\+xml;base64,/);
    expect(atob(img.getAttribute("src")?.split(",")[1] ?? "")).toBe(HEATMAP.svg);
    expect(container.querySelector("svg rect")).toBeNull();
    expect(container.innerHTML).not.toContain("<rect");
  });

  it("keeps non ASCII text of the map intact in the image", () => {
    const url = svgDataUrl("<svg>\u00f1 \u2265 0,5</svg>");
    const binary = atob(url.split(",")[1] ?? "");
    expect(new TextDecoder().decode(Uint8Array.from(binary, (c) => c.charCodeAt(0)))).toBe("<svg>\u00f1 \u2265 0,5</svg>");
  });

  it("marks the ideal result and the best point, with the legend of the best point", async () => {
    mockApi({ "GET /api/verdict/optimizations/818/heatmap": HEATMAP });
    const { container } = renderWithApp(<OptimizationBlock optId={818} ideal={IDEAL} />);
    await screen.findByRole("img");
    const ideal = container.querySelector('[data-marker="ideal"]') as HTMLElement;
    const best = container.querySelector('[data-marker="best"]') as HTMLElement;
    expect(ideal).not.toBeNull();
    expect(best).not.toBeNull();
    const layout = { cell: 48, left: 76, top: 28, gap: 8, bottom: 38 };
    // fast 12 is the 3rd of 4 columns; slow 160 is the 2nd of 3 rows, drawn from the top: the 2nd row counting from the bottom
    expect(ideal.style.left).toBe(`${layout.left + 2 * 48 + 24}px`);
    expect(ideal.style.top).toBe(`${layout.top + 1 * 48 + 24}px`);
    // fast 8 first column; slow 400 the last value of y: top row
    expect(best.style.left).toBe(`${layout.left + 24}px`);
    expect(best.style.top).toBe(`${layout.top + 24}px`);
    expect(screen.getByText(t("verdict.opt.legendBest"))).toBeInTheDocument();
    expect(screen.getByText(t("verdict.opt.legendIdeal"))).toBeInTheDocument();
    expect(screen.getByText(t("verdict.opt.ideal", { params: "fast 12, slow 160" }))).toBeInTheDocument();
    expect(screen.getByText(t("verdict.opt.plateauMean", { n: 14, mean: "1,18" }))).toBeInTheDocument();
    expect(screen.getByText(t("verdict.opt.bestEdge", { params: "fast 8, slow 400" }))).toBeInTheDocument();
  });

  it("the picture and its markers share the same size, so they line up", async () => {
    mockApi({ "GET /api/verdict/optimizations/818/heatmap": HEATMAP });
    const { container } = renderWithApp(<OptimizationBlock optId={818} ideal={IDEAL} />);
    const img = (await screen.findByRole("img")) as HTMLImageElement;
    const wrap = container.querySelector(".bt-heat") as HTMLElement;
    expect(wrap.style.width).toBe(`${76 + 4 * 48 + 8}px`);
    expect(wrap.style.height).toBe(`${28 + 3 * 48 + 38}px`);
    expect(img.getAttribute("width")).toBe(String(76 + 4 * 48 + 8));
  });

  it("a different pair of axes asks the server again with x and y", async () => {
    const calls = mockApi({ "GET /api/verdict/optimizations/818/heatmap": HEATMAP });
    renderWithApp(<OptimizationBlock optId={818} ideal={IDEAL} />);
    await screen.findByRole("img");
    await userEvent.selectOptions(screen.getByLabelText(t("verdict.opt.axisY")), "stop_atr");
    await waitFor(() => expect(calls.some((c) => c.path.includes("y=stop_atr"))).toBe(true));
  });

  it("reads the other fixed parameters, the counts and the notices of the search", async () => {
    mockApi({ "GET /api/verdict/optimizations/818/heatmap": { ...HEATMAP, warnings: ["many_trials"] } });
    renderWithApp(<OptimizationBlock optId={818} ideal={IDEAL} />);
    await screen.findByRole("img");
    expect(screen.getByText(t("verdict.opt.fixed", { list: "stop_atr = 2" }), { exact: false })).toBeInTheDocument();
    expect(screen.getByText(/Probadas 300 \| fallidas 4 \| repetidas 38/)).toBeInTheDocument();
    expect(screen.getByText(t("verdict.opt.warning", { warning: "many_trials" }), { exact: false })).toBeInTheDocument();
  });

  it("when there is no ideal result it says why, with the translated code", async () => {
    mockApi({ "GET /api/verdict/optimizations/818/heatmap": HEATMAP });
    renderWithApp(<OptimizationBlock optId={818} ideal={{ status: "not_found", params: null, plateau_size: null, run_id: null, reason: "plateau_fails_neighbors" }} />);
    await screen.findByRole("img");
    expect(screen.getByText(t("verdict.opt.notFound", { reason: t("verdict.ideal.reason.plateau_fails_neighbors") }))).toBeInTheDocument();
    expect(document.querySelector('[data-marker="ideal"]')).toBeNull();
  });

  it("without an optimization the block is empty with its reason", () => {
    renderWithApp(<OptimizationBlock optId={null} ideal={{ status: "not_found", params: null, plateau_size: null, run_id: null, reason: "no_optimization" }} />);
    const block = document.querySelector('[data-block="optimization"]') as HTMLElement;
    expect(within(block).getByText(t("verdict.opt.empty"))).toBeInTheDocument();
    expect(within(block).queryByRole("img")).toBeNull();
  });

  it("the position of a point is nearest the grid and null when a parameter is missing", () => {
    const layout = { cell: 48, left: 76, top: 28, gap: 8, bottom: 38 };
    expect(markerPosition(HEATMAP.heatmap, layout, { fast: 11.4, slow: 160 })?.left).toBe(76 + 2 * 48 + 24);
    expect(markerPosition(HEATMAP.heatmap, layout, { fast: 12 })).toBeNull();
    expect(markerPosition(HEATMAP.heatmap, layout, null)).toBeNull();
  });
});
