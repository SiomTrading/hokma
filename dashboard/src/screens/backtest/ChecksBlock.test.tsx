import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { mockApi } from "@/screens/system/testkit";
import { renderWithApp } from "@/test/utils";
import { ChecksBlock } from "./ChecksBlock";
import { resetOwnerGate, setOwnerBlocked } from "./api";
import { makeChecks, makeVerdict } from "./testkit";
import { PHASES } from "./types";

beforeEach(() => {
  setLocale("es");
  resetOwnerGate();
});
afterEach(() => vi.unstubAllGlobals());

const noop = () => undefined;

describe("Block B: the 24 checks (OPT-23)", () => {
  it("groups the 24 rows in the six phases of audit.PHASES, in order", () => {
    renderWithApp(<ChecksBlock verdict={makeVerdict()} ownerBlocked={false} onVerdict={noop} onAnswer={noop} />);
    const groups = screen.getAllByRole("region").filter((el) => PHASES.some((p) => el.getAttribute("aria-label") === t(`verdict.phase.${p}`)));
    expect(groups.map((g) => g.getAttribute("aria-label"))).toEqual(PHASES.map((p) => t(`verdict.phase.${p}`)));
    expect(screen.getAllByRole("listitem").filter((li) => li.classList.contains("bt-check"))).toHaveLength(24);
    expect(within(groups[4] as HTMLElement).getByText(t("verdict.check.pbo"))).toBeInTheDocument();
  });

  it("writes the main text from a template by key and state, with the code of the note choosing the variant", () => {
    renderWithApp(<ChecksBlock verdict={makeVerdict()} ownerBlocked={false} onVerdict={noop} onAnswer={noop} />);
    expect(screen.getByText(t("verdict.checkstate.costs_charged.fail.cost_share"))).toBeInTheDocument();
    expect(screen.getByText(t("verdict.checkstate.parameter_neighbors.warn.edge"))).toBeInTheDocument();
    expect(screen.getByText(t("verdict.checkstate.regimes.na"))).toBeInTheDocument();
    // the technical note is behind "Technical detail"
    expect(screen.getByText("cost_share: costs take 41% of the gross Sharpe; the limit is 33%")).toBeInTheDocument();
  });

  it("falls back to the generic text of the state when a key has no template for it", () => {
    const verdict = makeVerdict({ checks: makeChecks({ house_fill: { status: "warn", note: "" } }) });
    renderWithApp(<ChecksBlock verdict={verdict} ownerBlocked={false} onVerdict={noop} onAnswer={noop} />);
    expect(screen.getByText(t("verdict.checkstate.generic.warn"))).toBeInTheDocument();
  });

  it("Remove asks for an optional reason, sends it and shows the new verdict with the row struck and its date", async () => {
    const after = makeVerdict({
      verdict: "review",
      checks: makeChecks({
        costs_charged: { status: "fail", note: "cost_share: x", excluded: true },
        pbo: { status: "warn", note: "PBO 0.3" },
      }),
      excluded: [{ key: "costs_charged", reason: "costs are fine for this book", by: "owner", at: "2026-11-25T19:20:00Z" }],
    });
    const calls = mockApi({ "POST /api/verdict/41/exclude": after });
    const onVerdict = vi.fn();
    const { rerender } = renderWithApp(<ChecksBlock verdict={makeVerdict()} ownerBlocked={false} onVerdict={onVerdict} onAnswer={noop} />);
    await userEvent.click(screen.getByRole("button", { name: new RegExp(`^${t("verdict.row.exclude")}: ${t("verdict.check.costs_charged")}`) }));
    await userEvent.type(screen.getByLabelText(t("verdict.row.reasonLabel")), "costs are fine for this book");
    await userEvent.click(screen.getByRole("button", { name: t("verdict.row.confirmExclude") }));
    await waitFor(() => expect(onVerdict).toHaveBeenCalledWith(after));
    expect(calls.find((c) => c.path === "/api/verdict/41/exclude")?.body).toEqual({ keys: ["costs_charged"], reason: "costs are fine for this book" });

    rerender(
      <div className="hk">
        <ChecksBlock verdict={after} ownerBlocked={false} onVerdict={onVerdict} onAnswer={noop} />
      </div>,
    );
    const row = document.querySelector('[data-check="costs_charged"]') as HTMLElement;
    expect(row).toHaveAttribute("data-status", "excluded");
    expect(row.querySelector(".bt-check-name")).toHaveClass("bt-struck");
    expect(within(row).getByText(/quitada por ti el .*2026/)).toBeInTheDocument();
    expect(within(row).getByText(/costs are fine for this book/)).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: new RegExp(t("verdict.row.include")) })).toBeEnabled();
  });

  it("Remove without a reason sends only the keys", async () => {
    const calls = mockApi({ "POST /api/verdict/41/exclude": makeVerdict({ verdict: "review" }) });
    renderWithApp(<ChecksBlock verdict={makeVerdict()} ownerBlocked={false} onVerdict={noop} onAnswer={noop} />);
    await userEvent.click(screen.getByRole("button", { name: new RegExp(`^${t("verdict.row.exclude")}: ${t("verdict.check.pbo")}`) }));
    await userEvent.click(screen.getByRole("button", { name: t("verdict.row.confirmExclude") }));
    await waitFor(() => expect(calls.length).toBe(1));
    expect(calls[0]?.body).toEqual({ keys: ["pbo"] });
  });

  it("Put back sends include with the key and hands over the new verdict", async () => {
    const excludedVerdict = makeVerdict({
      checks: makeChecks({ holdout_untouched: { status: "unknown", excluded: true } }),
      excluded: [{ key: "holdout_untouched", reason: null, by: "owner", at: "2026-11-25T10:00:00Z" }],
    });
    const restored = makeVerdict();
    const calls = mockApi({ "POST /api/verdict/41/include": restored });
    const onVerdict = vi.fn();
    renderWithApp(<ChecksBlock verdict={excludedVerdict} ownerBlocked={false} onVerdict={onVerdict} onAnswer={noop} />);
    await userEvent.click(screen.getByRole("button", { name: new RegExp(`^${t("verdict.row.include")}: `) }));
    await waitFor(() => expect(onVerdict).toHaveBeenCalledWith(restored));
    expect(calls[0]?.body).toEqual({ keys: ["holdout_untouched"] });
  });

  it("without the owner confirmation Remove, Put back and Answer are disabled", () => {
    const verdict = makeVerdict({
      checks: makeChecks({ holdout_untouched: { status: "unknown", excluded: true }, survivorship_free: { status: "unknown" } }),
      excluded: [{ key: "holdout_untouched", reason: null, by: "owner", at: "2026-11-25T10:00:00Z" }],
    });
    renderWithApp(<ChecksBlock verdict={verdict} ownerBlocked onVerdict={noop} onAnswer={noop} />);
    for (const button of screen.getAllByRole("button", { name: new RegExp(`^(${t("verdict.row.exclude")}|${t("verdict.row.include")}|${t("verdict.row.answer")}): `) })) {
      expect(button).toBeDisabled();
    }
    expect(screen.getAllByRole("button", { name: new RegExp(`^${t("verdict.row.exclude")}: `) }).length).toBeGreaterThan(0);
  });

  it("a 403 without the confirmation turns the screen into read mode", async () => {
    mockApi({ "POST /api/verdict/41/exclude": () => new Response(JSON.stringify({ code: "owner_confirmation_required", detail: "no" }), { status: 403, headers: { "Content-Type": "application/json" } }) });
    renderWithApp(<ChecksBlock verdict={makeVerdict()} ownerBlocked={false} onVerdict={noop} onAnswer={noop} />);
    await userEvent.click(screen.getByRole("button", { name: new RegExp(`^${t("verdict.row.exclude")}: ${t("verdict.check.pbo")}`) }));
    await userEvent.click(screen.getByRole("button", { name: t("verdict.row.confirmExclude") }));
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    const { useOwnerBlockedForTest } = await import("./testOwner");
    expect(useOwnerBlockedForTest()).toBe(true);
  });

  it("Answer shows only on a check that waits for a question and opens the sheet", async () => {
    const onAnswer = vi.fn();
    const verdict = makeVerdict({ checks: makeChecks({ survivorship_free: { status: "unknown" } }) });
    renderWithApp(<ChecksBlock verdict={verdict} ownerBlocked={false} onVerdict={noop} onAnswer={onAnswer} />);
    const buttons = screen.getAllByRole("button", { name: new RegExp(`^${t("verdict.row.answer")}: `) });
    expect(buttons).toHaveLength(1);
    await userEvent.click(buttons[0] as HTMLElement);
    expect(onAnswer).toHaveBeenCalledTimes(1);
  });

  it("a check the owner marked not applicable has no Put back button", () => {
    const verdict = makeVerdict({
      checks: makeChecks({ survivorship_free: { status: "unknown", excluded: true } }),
      excluded: [{ key: "survivorship_free", reason: "owner_said_na", by: "system", at: "2026-11-25T10:00:00Z" }],
    });
    renderWithApp(<ChecksBlock verdict={verdict} ownerBlocked={false} onVerdict={noop} onAnswer={noop} />);
    const row = document.querySelector('[data-check="survivorship_free"]') as HTMLElement;
    expect(within(row).getByText(t("verdict.row.excludedNa"))).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: new RegExp(t("verdict.row.include")) })).toBeNull();
  });

  it("an owner call that goes through clears the read mode", async () => {
    setOwnerBlocked(true);
    mockApi({ "POST /api/verdict/41/include": makeVerdict() });
    const { verdictApi } = await import("./api");
    await verdictApi.include(41, ["pbo"]);
    const { useOwnerBlockedForTest } = await import("./testOwner");
    expect(useOwnerBlockedForTest()).toBe(false);
  });
});

describe("A check the system took out because it does not apply", () => {
  it("says so, does not say the owner removed it, and has no Put back button (the route refuses it)", () => {
    const verdict = makeVerdict({
      checks: makeChecks({ survivorship_free: { status: "unknown", excluded: true } }),
      excluded: [{ key: "survivorship_free", reason: "not_applicable:single_instrument", by: "system", at: "2026-11-25T10:00:00Z" }],
    });
    renderWithApp(<ChecksBlock verdict={verdict} ownerBlocked={false} onVerdict={noop} onAnswer={noop} />);
    const row = document.querySelector('[data-check="survivorship_free"]') as HTMLElement;
    expect(within(row).getByText(t("verdict.row.excludedAuto"))).toBeInTheDocument();
    expect(row.textContent).not.toContain("single_instrument");
    expect(within(row).getByText(t("verdict.status.na"))).toBeInTheDocument();
    expect(within(row).queryByText(/quitada por ti|removed by you/)).toBeNull();
    expect(within(row).queryByRole("button", { name: new RegExp(t("verdict.row.include")) })).toBeNull();
  });
});
