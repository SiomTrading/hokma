import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { renderWithApp } from "@/test/utils";
import { VerdictBlock } from "./VerdictBlock";
import { makeVerdict } from "./testkit";
import { VERDICT_STATES } from "./types";

beforeEach(() => setLocale("es"));

describe("Block A: verdict with three sentences (OPT-22)", () => {
  it.each(VERDICT_STATES.map((state) => [state]))("%s has its own color variant and always says Suggestion", (state) => {
    renderWithApp(<VerdictBlock verdict={makeVerdict({ verdict: state, audit_verdict: state, open_questions: [] })} ownerBlocked={false} onAnswer={() => undefined} noWalkForward={false} />);
    const box = screen.getByRole("status");
    expect(box).toHaveAttribute("data-variant", state);
    expect(box.textContent).toContain("Sugerencia");
    expect(screen.getByText(t(`verdict.variant.${state}`))).toBeInTheDocument();
  });

  it("shows the three sentences the server sent, in order, and that it blocks nothing", () => {
    const verdict = makeVerdict();
    renderWithApp(<VerdictBlock verdict={verdict} ownerBlocked={false} onAnswer={() => undefined} noWalkForward={false} />);
    const items = screen.getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual(verdict.sentences);
    expect(screen.getByText(t("verdict.advisory"))).toBeInTheDocument();
  });

  it("the four texts differ in English too and keep the word Suggestion", () => {
    setLocale("en");
    for (const state of VERDICT_STATES) expect(t(`verdict.variant.${state}`)).toContain("Suggestion");
  });

  it("says the audit alone would have said something else when checks were removed", () => {
    renderWithApp(<VerdictBlock verdict={makeVerdict({ verdict: "go", audit_verdict: "no_go" })} ownerBlocked={false} onAnswer={() => undefined} noWalkForward={false} />);
    expect(screen.getByText(t("verdict.rawVerdict", { variant: t("verdict.variant.no_go") }))).toBeInTheDocument();
  });

  it("says there is no walk-forward when the run has none", () => {
    renderWithApp(<VerdictBlock verdict={makeVerdict({ verdict: "needs_evidence" })} ownerBlocked={false} onAnswer={() => undefined} noWalkForward />);
    expect(screen.getByText(t("verdict.screen.noWalkForward"))).toBeInTheDocument();
  });

  it("offers to answer the open questions, and not without the owner confirmation", async () => {
    const onAnswer = vi.fn();
    const { rerender } = renderWithApp(<VerdictBlock verdict={makeVerdict()} ownerBlocked={false} onAnswer={onAnswer} noWalkForward={false} />);
    expect(screen.getByText(t("verdict.questions.open.one"))).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: t("verdict.questions.answer") }));
    expect(onAnswer).toHaveBeenCalledTimes(1);
    rerender(
      <div className="hk">
        <VerdictBlock verdict={makeVerdict()} ownerBlocked onAnswer={onAnswer} noWalkForward={false} />
      </div>,
    );
    expect(screen.getByRole("button", { name: t("verdict.questions.answer") })).toBeDisabled();
  });

  it("hides the question line when nothing is open", () => {
    renderWithApp(<VerdictBlock verdict={makeVerdict({ open_questions: [] })} ownerBlocked={false} onAnswer={() => undefined} noWalkForward={false} />);
    expect(screen.queryByRole("button", { name: t("verdict.questions.answer") })).toBeNull();
  });
});
