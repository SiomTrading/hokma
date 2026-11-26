import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { NoticesBlock, collectNotices } from "./NoticesBlock";
import { SuggestionsBlock, buildCopyText } from "./SuggestionsBlock";
import { NOTICE_CODES } from "./format";
import { MONTECARLO, SUMMARY, WALKFORWARD, makeVerdict } from "./testkit";

beforeEach(() => setLocale("es"));
afterEach(() => vi.unstubAllGlobals());

describe("Block H: suggestions for the creator (OPT-28)", () => {
  it("lists them in order with the text of the server and a copy button each, but none for the owner's question", () => {
    renderWithApp(<SuggestionsBlock suggestions={makeVerdict().suggestions} />);
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(4);
    expect(items[0]).toHaveTextContent("Trade less or with a wider target.");
    expect(screen.getAllByRole("button", { name: new RegExp(t("verdict.sug.copy")) })).toHaveLength(3);
    expect(screen.getByText(t("verdict.sug.forOwner"))).toBeInTheDocument();
  });

  it("copies English text that carries the action code", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const suggestions = makeVerdict().suggestions;
    renderWithApp(<SuggestionsBlock suggestions={suggestions} />);
    await userEvent.click(screen.getAllByRole("button", { name: new RegExp(t("verdict.sug.copy")) })[1] as HTMLElement);
    expect(writeText).toHaveBeenCalledTimes(1);
    const text = writeText.mock.calls[0]?.[0] as string;
    expect(text).toContain("action: reduce_overfit");
    expect(text).toContain("Fewer parameters or shorter ranges; more history.");
    expect(text).toContain('"pbo":0.58');
    expect(text).not.toMatch(/[\u00e1\u00e9\u00ed\u00f3\u00fa\u00f1]/);
    expect(await screen.findByText(t("verdict.sug.copied"))).toBeInTheDocument();
  });

  it("builds a text for every action of the section 8 table and falls back to the server text for a new one", () => {
    const actions = ["use_point_in_time_data", "include_delisted", "apply_corporate_actions", "fix_lookahead", "clean_data", "count_trials", "tune_in_sample", "reserve_holdout", "lengthen_history", "charge_costs", "reduce_turnover", "model_impact", "charge_financing", "use_house_fill", "run_walk_forward", "deflate_sharpe", "beat_chance", "raise_dsr", "reduce_overfit", "seek_plateau", "widen_range", "regime_filter", "audit_for_bug", "diversify_trades", "stabilize_oos", "split_costs", "report_drawdown"];
    for (const action of actions) {
      const text = buildCopyText({ check: 1, key: "pbo", status: "fail", action, text: "SERVER TEXT", params: {} });
      expect(text).toContain(`action: ${action}`);
      expect(text).not.toContain("SERVER TEXT");
    }
    expect(buildCopyText({ check: 1, key: "pbo", status: "fail", action: "brand_new_action", text: "SERVER TEXT", params: {} })).toContain("SERVER TEXT");
  });

  it("says there are no changes when there are none", () => {
    renderWithApp(<SuggestionsBlock suggestions={[]} />);
    expect(screen.getByText(t("verdict.sug.empty"))).toBeInTheDocument();
  });
});

describe("Block I: notices in plain language (OPT-29)", () => {
  it.each([...NOTICE_CODES])("translates %s", (code) => {
    const raw = code === "ambiguous_bars" ? "ambiguous_bars:3" : code === "analysis_part_missing" ? "analysis_part_missing:plateaus" : code === "edge_of_space" ? "edge_of_space:slow" : ["presweep_top_k", "constraint_rejections", "failed_trials"].includes(code) ? `${code}:3` : code;
    renderWithApp(<NoticesBlock codes={[raw]} />);
    const text = screen.getByText(t(`verdict.notice.${code}`, { n: "3", part: "plateaus", param: "slow" })).textContent ?? "";
    expect(text).not.toContain("{");
    expect(screen.queryByText(t("verdict.notice.unknown", { code: "" }).trim())).toBeNull();
  });

  it("gives the texts of section 7.2 I word for word", () => {
    expect(t("verdict.notice.many_trials")).toContain("300");
    expect(t("verdict.notice.ambiguous_bars", { n: 3 })).toMatch(/^3 .*stop/);
    expect(t("verdict.notice.analysis_part_missing", { part: "plateaus" })).toMatch(/plateaus\.$/);
  });

  it("shows the count of ambiguous bars and the part that is missing from the code", () => {
    renderWithApp(<NoticesBlock codes={["ambiguous_bars:3", "analysis_part_missing:plateaus"]} />);
    expect(screen.getByText(t("verdict.notice.ambiguous_bars", { n: 3 }))).toBeInTheDocument();
    expect(screen.getByText(t("verdict.notice.analysis_part_missing", { part: "plateaus" }))).toBeInTheDocument();
  });

  it("a code it does not know is a technical notice with the original text", () => {
    renderWithApp(<NoticesBlock codes={["something_new:x"]} />);
    expect(screen.getByText(t("verdict.notice.unknown", { code: "something_new:x" }))).toBeInTheDocument();
    expect(document.querySelector('[data-known="no"]')).not.toBeNull();
  });

  it("says there are no notices when there are none", () => {
    renderWithApp(<NoticesBlock codes={[]} />);
    expect(screen.getByText(t("verdict.notice.empty"))).toBeInTheDocument();
  });

  it("collects the codes of the verdict and of its runs once each", () => {
    const verdict = makeVerdict({ notices: ["many_trials", "data_changed"], runs: { run: 812, walkforward: 820, optimize: 818, montecarlo: null } });
    const codes = collectNotices({
      verdict,
      summary: { ...SUMMARY, warnings: ["many_trials"] },
      walkforward: { ...WALKFORWARD, warnings: ["in_sample_not_positive"] },
      montecarlo: undefined,
    });
    expect(codes).toEqual(["many_trials", "data_changed", "ambiguous_bars:3", "in_sample_not_positive", "monte_carlo_missing"]);
  });

  it("does not say Monte Carlo is missing when it exists", () => {
    const codes = collectNotices({ verdict: makeVerdict(), summary: { ...SUMMARY, ambiguous_bars: 0 }, montecarlo: MONTECARLO });
    expect(codes).toEqual([]);
  });
});
