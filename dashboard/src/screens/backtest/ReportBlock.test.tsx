import { screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { failure, mockApi } from "@/screens/system/testkit";
import { renderWithApp } from "@/test/utils";
import { ReportBlock } from "./ReportBlock";
import { html } from "./testkit";

beforeEach(() => setLocale("es"));
afterEach(() => vi.unstubAllGlobals());

const REPORT = "<html><body><svg><title>equity</title></svg><p>Retornos por mes</p></body></html>";

describe("Block D: report in an iframe without scripts (OPT-25)", () => {
  it("puts the report in an iframe with sandbox empty and srcdoc, and no way to run scripts", async () => {
    const calls = mockApi({ "GET /api/verdict/runs/812/report": () => html(REPORT) });
    renderWithApp(<ReportBlock runId={812} />);
    const frame = await screen.findByTitle(t("verdict.report.title", { run: 812 }));
    expect(frame.tagName).toBe("IFRAME");
    expect(frame.getAttribute("sandbox")).toBe("");
    expect(frame.getAttribute("srcdoc")).toBe(REPORT);
    expect(frame.getAttribute("sandbox")).not.toContain("allow-scripts");
    expect(frame.getAttribute("sandbox")).not.toContain("allow-same-origin");
    expect(frame.getAttribute("src")).toBeNull();
    expect(calls[0]?.path).toBe("/api/verdict/runs/812/report");
  });

  it("never inserts the report into the page itself", async () => {
    mockApi({ "GET /api/verdict/runs/812/report": () => html(REPORT) });
    const { container } = renderWithApp(<ReportBlock runId={812} />);
    await screen.findByTitle(t("verdict.report.title", { run: 812 }));
    expect(container.querySelector("svg")).toBeNull();
    expect(container.textContent).not.toContain("Retornos por mes");
  });

  it("says there is no report when the server has none", async () => {
    mockApi({ "GET /api/verdict/runs/812/report": () => failure(404, "none") });
    renderWithApp(<ReportBlock runId={812} />);
    await waitFor(() => expect(screen.getByText(t("verdict.report.empty"))).toBeInTheDocument());
  });
});
