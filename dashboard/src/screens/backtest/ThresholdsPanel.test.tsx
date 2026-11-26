import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useApiQuery } from "@/api";
import { setLocale, t } from "@/i18n";
import { failure, mockApi } from "@/screens/system/testkit";
import type { Call } from "@/screens/system/testkit";
import { renderWithApp } from "@/test/utils";
import { ThresholdsPanel } from "./ThresholdsPanel";
import { resetOwnerGate } from "./api";
import { MEASURES } from "./measures";
import { THRESHOLDS_V4, THRESHOLD_HISTORY, json, makeVerdict } from "./testkit";
import type { LineId, ThresholdSet, Verdict } from "./types";

beforeEach(() => {
  setLocale("es");
  resetOwnerGate();
});
afterEach(() => vi.unstubAllGlobals());

function Harness({ line = "DT", verdict = makeVerdict(), ownerBlocked = false, onRecomputed = () => undefined }: { line?: LineId; verdict?: Verdict | null; ownerBlocked?: boolean; onRecomputed?: (v: Verdict) => void }) {
  const current = useApiQuery<ThresholdSet>(`/api/verdict/thresholds/${line}`);
  return <ThresholdsPanel line={line} current={current} verdict={verdict} ownerBlocked={ownerBlocked} onChanged={current.reload} onRecomputed={onRecomputed} onClose={() => undefined} />;
}

/** A server that remembers: PUT, restore and copy write a new version, GET returns the latest. */
function server(extra: Record<string, unknown> = {}) {
  const sets: ThresholdSet[] = [...THRESHOLD_HISTORY];
  const latest = () => sets[sets.length - 1] as ThresholdSet;
  const add = (values: ThresholdSet["values"], reason: string | null, restoredFrom?: number): ThresholdSet => {
    const made: ThresholdSet = { ...latest(), id: latest().id + 1, version: latest().version + 1, values, reason, changed_by: "owner", created_at: "2026-11-25T20:00:00Z", restored_from: restoredFrom ?? null };
    sets.push(made);
    return made;
  };
  const routes: Record<string, unknown> = {
    "GET /api/verdict/thresholds/DT": () => latest(),
    "GET /api/verdict/thresholds/DT/history": () => [...sets],
    "PUT /api/verdict/thresholds/DT": (call: Call) => {
      const body = call.body as { values: ThresholdSet["values"]; reason?: string };
      return add(body.values, body.reason ?? null);
    },
    "POST /api/verdict/thresholds/DT/restore/12": () => add(sets[1]?.values ?? {}, null, 12),
    ...extra,
  };
  return routes;
}

const field = (id: string, kind: "value" | "slider") => screen.getByLabelText(t(`verdict.th.${kind}`, { label: t(`verdict.measure.${id}.label`) }));

describe("Thresholds panel (OPT-30)", () => {
  it("shows the 13 measures with a slider and a number, or a switch, at the values that are in force", async () => {
    mockApi(server());
    renderWithApp(<Harness />);
    await screen.findByText(t("verdict.measure.M1.label"));
    expect(document.querySelectorAll(".bt-measure")).toHaveLength(13);
    for (const meta of MEASURES) {
      const label = t(`verdict.measure.${meta.id}.label`);
      expect(screen.getByText(label)).toBeInTheDocument();
      if (meta.kind === "toggle") expect(screen.getByRole("switch", { name: label })).toHaveAttribute("aria-checked", "true");
      else {
        const number = field(meta.id, "value") as HTMLInputElement;
        expect(Number(number.value)).toBe(THRESHOLDS_V4.values[meta.key]);
        expect(number.min).toBe(String(meta.low));
        expect(number.max).toBe(String(meta.high));
        expect(field(meta.id, "slider")).toHaveAttribute("type", "range");
      }
    }
    expect(screen.getByRole("region", { name: t("verdict.th.title", { line: "DT", version: 4 }) })).toBeInTheDocument();
  });

  it("the question mark opens what it measures, why it matters, raise, lower and an example", async () => {
    mockApi(server());
    renderWithApp(<Harness />);
    await screen.findByText(t("verdict.measure.M3.label"));
    const help = screen.getByRole("button", { name: t("verdict.th.help", { label: t("verdict.measure.M3.label") }) });
    expect(help).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(help);
    expect(help).toHaveAttribute("aria-expanded", "true");
    const row = document.querySelector('[data-measure="M3"]') as HTMLElement;
    for (const field of ["what", "why", "raise", "lower", "example"]) {
      expect(within(row).getByText(t(`verdict.measure.M3.${field}`))).toBeInTheDocument();
      expect(within(row).getByText(t(`verdict.th.${field}`))).toBeInTheDocument();
    }
    await userEvent.click(help);
    expect(within(row).queryByText(t("verdict.measure.M3.what"))).toBeNull();
  });

  it("a change stays a draft until Save; Save sends all the values with the reason and creates a version", async () => {
    const calls = mockApi(server());
    renderWithApp(<Harness />);
    await screen.findByText(t("verdict.measure.M5.label"));
    const save = screen.getByRole("button", { name: t("verdict.th.save") });
    expect(save).toBeDisabled();
    fireEvent.change(field("M5", "value"), { target: { value: "0.4" } });
    expect(save).toBeEnabled();
    expect(calls.some((c) => c.method === "PUT")).toBe(false);
    await userEvent.type(screen.getByLabelText(t("verdict.th.reason")), "books with few trades");
    await userEvent.click(save);
    await screen.findByText(t("verdict.th.saved", { version: 5 }));
    const put = calls.find((c) => c.method === "PUT");
    expect(put?.path).toBe("/api/verdict/thresholds/DT");
    const body = put?.body as { values: Record<string, unknown>; reason: string };
    expect(body.reason).toBe("books with few trades");
    expect(Object.keys(body.values).sort()).toEqual(MEASURES.map((m) => m.key).sort());
    expect(body.values.max_cost_share).toBe(0.4);
    expect(body.values.min_dsr).toBe(0.95);
    await waitFor(() => expect(screen.getByRole("region", { name: t("verdict.th.title", { line: "DT", version: 5 }) })).toBeInTheDocument());
  });

  it("Recalculate is offered after saving but does not run by itself, and runs once when pressed", async () => {
    const recomputed = makeVerdict({ thresholds: { line: "DT", version: 5 }, verdict: "review" });
    const calls = mockApi(server({ "POST /api/verdict/41/recompute": recomputed }));
    const onRecomputed = vi.fn();
    renderWithApp(<Harness onRecomputed={onRecomputed} />);
    await screen.findByText(t("verdict.measure.M5.label"));
    expect(screen.queryByRole("button", { name: t("verdict.th.recompute") })).toBeNull();
    fireEvent.change(field("M5", "value"), { target: { value: "0.4" } });
    await userEvent.click(screen.getByRole("button", { name: t("verdict.th.save") }));
    const recompute = await screen.findByRole("button", { name: t("verdict.th.recompute") });
    expect(screen.getByText(t("verdict.th.pending", { old: 4, new: 5 }))).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(calls.some((c) => c.path.endsWith("/recompute"))).toBe(false);
    await userEvent.click(recompute);
    await waitFor(() => expect(onRecomputed).toHaveBeenCalledWith(recomputed));
    expect(calls.filter((c) => c.path.endsWith("/recompute"))).toHaveLength(1);
    expect(calls.find((c) => c.path.endsWith("/recompute"))?.body).toEqual({ cause: "thresholds" });
  });

  it("a value outside the range is not written: the control keeps the last valid value and Save stays off", async () => {
    mockApi(server());
    renderWithApp(<Harness />);
    await screen.findByText(t("verdict.measure.M3.label"));
    const number = field("M3", "value") as HTMLInputElement;
    await userEvent.clear(number);
    await userEvent.type(number, "7");
    expect(number).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("alert")).toHaveTextContent(t("verdict.th.invalid", { low: "0.5", high: "0.99" }));
    expect(screen.getByRole("button", { name: t("verdict.th.save") })).toBeDisabled();
    fireEvent.blur(number);
    expect(number.value).toBe("0.99");
    expect(screen.getByRole("button", { name: t("verdict.th.save") })).toBeEnabled();
  });

  it("the yellow of the PBO cannot be above the red", async () => {
    mockApi(server());
    renderWithApp(<Harness />);
    await screen.findByText(t("verdict.measure.M1.label"));
    fireEvent.change(field("M1", "value"), { target: { value: "0.6" } });
    expect(screen.getByText(t("verdict.th.pboOrder"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("verdict.th.save") })).toBeDisabled();
  });

  it("the switch of the border mark is a draft too and Discard brings the saved values back", async () => {
    mockApi(server());
    renderWithApp(<Harness />);
    const toggle = await screen.findByRole("switch", { name: t("verdict.measure.M6.label") });
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("button", { name: t("verdict.th.save") })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: t("verdict.th.discard") }));
    expect(toggle).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("button", { name: t("verdict.th.save") })).toBeDisabled();
  });

  it("the history shows who, when, why and each measure before and after, and the first version has none", async () => {
    mockApi(server());
    renderWithApp(<Harness />);
    await screen.findByText(t("verdict.measure.M1.label"));
    await userEvent.click(screen.getByRole("button", { name: t("verdict.th.history") }));
    await screen.findByText(t("verdict.th.firstVersion"));
    const items = Array.from(document.querySelectorAll(".bt-history-item")) as HTMLElement[];
    expect(items.map((i) => i.dataset.version)).toEqual(["4", "3", "2", "1"]);
    const label5 = t("verdict.measure.M5.label");
    const label3 = t("verdict.measure.M3.label");
    expect(items[0]).toHaveTextContent(t("verdict.th.change", { label: label5, before: "0,30", after: "0,33" }));
    expect(items[0]).toHaveTextContent(t("verdict.th.current"));
    expect(items[0]).toHaveTextContent("more room for costs");
    expect(items[1]).toHaveTextContent(t("verdict.th.change", { label: label3, before: "0,90", after: "0,95" }));
    expect(items[2]).toHaveTextContent(t("verdict.th.change", { label: label5, before: "0,25", after: "0,30" }));
    expect(items[3]).toHaveTextContent(t("verdict.th.by.seed"));
    expect(items[0]?.querySelector("button")).toBeNull();
  });

  it("Go back to this version creates a new version with the old values", async () => {
    const calls = mockApi(server());
    renderWithApp(<Harness />);
    await screen.findByText(t("verdict.measure.M1.label"));
    await userEvent.click(screen.getByRole("button", { name: t("verdict.th.history") }));
    await screen.findByText(t("verdict.th.firstVersion"));
    await userEvent.click(screen.getByRole("button", { name: `${t("verdict.th.restore")}: ${t("verdict.th.version", { version: 2 })}` }));
    await screen.findByText(t("verdict.th.restored", { version: 5 }));
    expect(calls.some((c) => c.method === "POST" && c.path === "/api/verdict/thresholds/DT/restore/12")).toBe(true);
    await waitFor(() => expect(document.querySelectorAll(".bt-history-item")).toHaveLength(5));
  });

  it("without the owner confirmation the panel is read only", async () => {
    mockApi(server());
    renderWithApp(<Harness ownerBlocked />);
    await screen.findByText(t("verdict.measure.M1.label"));
    expect(screen.getByText(t("verdict.owner.missing"), { exact: false })).toBeInTheDocument();
    expect(field("M3", "value")).toBeDisabled();
    expect(field("M3", "slider")).toBeDisabled();
    expect(screen.getByRole("switch", { name: t("verdict.measure.M6.label") })).toBeDisabled();
    expect(screen.getByRole("button", { name: t("verdict.th.save") })).toBeDisabled();
    // still readable
    await userEvent.click(screen.getByRole("button", { name: t("verdict.th.help", { label: t("verdict.measure.M3.label") }) }));
    expect(screen.getByText(t("verdict.measure.M3.what"))).toBeInTheDocument();
  });

  it("a line with no thresholds (LP, RF) offers to copy those of SWC", async () => {
    const calls = mockApi({
      "GET /api/verdict/thresholds/LP": () => json({ code: "bad_input", reason: "no_thresholds", detail: "no thresholds" }, 400),
      "POST /api/verdict/thresholds/LP/copy-from/SWC": { ...THRESHOLDS_V4, line: "LP", version: 1 },
    });
    renderWithApp(<Harness line="LP" verdict={null} />);
    expect(await screen.findByText(t("verdict.screen.noThresholds", { line: "LP" }))).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: t("verdict.screen.copyThresholds") }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/verdict/thresholds/LP/copy-from/SWC")).toBe(true));
  });

  it("shows the message of the server when a save is refused", async () => {
    mockApi(server({ "PUT /api/verdict/thresholds/DT": () => failure(400, "pbo_warn is above pbo_reject") }));
    renderWithApp(<Harness />);
    await screen.findByText(t("verdict.measure.M5.label"));
    fireEvent.change(field("M5", "value"), { target: { value: "0.4" } });
    await userEvent.click(screen.getByRole("button", { name: t("verdict.th.save") }));
    expect(await screen.findByText("pbo_warn is above pbo_reject")).toBeInTheDocument();
  });
});

describe("A save that the server did not write", () => {
  it("shows that there was nothing to save when the server answers the current set with unchanged", async () => {
    mockApi(server({ "PUT /api/verdict/thresholds/DT": () => ({ ...THRESHOLDS_V4, unchanged: true }) }));
    renderWithApp(<Harness />);
    await screen.findByText(t("verdict.measure.M5.label"));
    fireEvent.change(field("M5", "value"), { target: { value: "0.4" } });
    await userEvent.click(screen.getByRole("button", { name: t("verdict.th.save") }));
    expect(await screen.findByText(t("verdict.th.unchanged"))).toBeInTheDocument();
    expect(screen.queryByText(t("verdict.th.saved", { version: THRESHOLDS_V4.version }))).toBeNull();
  });
});
