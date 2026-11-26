import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLocale, t } from "@/i18n";
import { mockApi } from "@/screens/system/testkit";
import { renderWithApp } from "@/test/utils";
import { QuestionsSheet } from "./QuestionsSheet";
import { resetOwnerGate } from "./api";
import { QUESTIONS, makeVerdict } from "./testkit";
import type { QuestionState } from "./types";

beforeEach(() => {
  setLocale("es");
  resetOwnerGate();
});
afterEach(() => vi.unstubAllGlobals());

function setup(options: { ownerBlocked?: boolean; after?: QuestionState[] } = {}) {
  const newVerdict = makeVerdict({ verdict: "review", open_questions: [{ key: "survivorship_free", scope: "data", subject: "CME:NQ" }] });
  let questions = QUESTIONS;
  const calls = mockApi({
    "GET /api/verdict/ema_cross_nq/3/questions": () => questions,
    "POST /api/verdict/ema_cross_nq/3/answers": () => {
      if (options.after) questions = options.after;
      return { saved: 1 };
    },
    "POST /api/verdict/41/recompute": newVerdict,
  });
  const onVerdict = vi.fn();
  renderWithApp(<QuestionsSheet strategy="ema_cross_nq" version={3} verdict={makeVerdict()} ownerBlocked={options.ownerBlocked ?? false} onVerdict={onVerdict} onClose={() => undefined} />);
  return { calls, onVerdict, newVerdict };
}

const AFTER: QuestionState[] = QUESTIONS.map((q) => (q.key === "gaps_checked" ? { ...q, status: "answered" as const, answer: "yes" as const } : q));

describe("Questions sheet (OPT-31)", () => {
  it("lists the open questions with their text, the suggestion, the scope and the answered ones apart", async () => {
    setup();
    await screen.findByText(new RegExp(t("verdict.question.survivorship_free")));
    expect(screen.getByRole("region", { name: t("verdict.q.title", { n: 3 }) })).toBeInTheDocument();
    expect(screen.getAllByRole("group")).toHaveLength(3);
    expect(screen.getAllByRole("radiogroup")).toHaveLength(3);
    expect(screen.getByText(t("verdict.q.suggested", { answer: t("verdict.q.yes") }))).toBeInTheDocument();
    expect(screen.getAllByText(t("verdict.q.scopeData", { subject: "CME:NQ" })).length).toBe(2);
    const answered = screen.getByRole("region", { name: t("verdict.q.answered") });
    expect(within(answered).getByText(t("verdict.q.yes"))).toBeInTheDocument();
    expect(within(answered).getByRole("button", { name: t("verdict.q.change") })).toBeInTheDocument();
    for (const question of document.querySelectorAll(".bt-question")) {
      expect(within(question as HTMLElement).getAllByRole("radio")).toHaveLength(3);
    }
  });

  it("Confirm suggestions answers only the questions that have one, then recomputes with cause answers", async () => {
    const { calls, onVerdict, newVerdict } = setup({ after: AFTER });
    await screen.findByText(new RegExp(t("verdict.question.survivorship_free")));
    await userEvent.click(screen.getByRole("button", { name: t("verdict.q.confirm") }));
    await waitFor(() => expect(onVerdict).toHaveBeenCalledWith(newVerdict));
    const answers = calls.find((c) => c.path.endsWith("/answers"));
    expect(answers?.body).toEqual({ answers: [{ key: "gaps_checked", value: "yes" }] });
    expect(calls.find((c) => c.path === "/api/verdict/41/recompute")?.body).toEqual({ cause: "answers" });
    const order = calls.filter((c) => c.method === "POST").map((c) => c.path);
    expect(order).toEqual(["/api/verdict/ema_cross_nq/3/answers", "/api/verdict/41/recompute"]);
    await screen.findByText(t("verdict.q.saved"));
    // the questions that had no suggestion stay open
    await waitFor(() => expect(document.querySelectorAll(".bt-question")).toHaveLength(2));
  });

  it("Save answers sends the chosen ones with their notes and leaves the rest alone", async () => {
    const { calls, onVerdict } = setup();
    await screen.findByText(new RegExp(t("verdict.question.survivorship_free")));
    const save = screen.getByRole("button", { name: t("verdict.q.save") });
    expect(save).toBeDisabled();
    const holdout = document.querySelector('[data-question="holdout_untouched"]') as HTMLElement;
    await userEvent.click(within(holdout).getByRole("radio", { name: t("verdict.q.no") }));
    await userEvent.type(within(holdout).getByLabelText(t("verdict.q.note")), "no holdout on purpose");
    const survivors = document.querySelector('[data-question="survivorship_free"]') as HTMLElement;
    await userEvent.click(within(survivors).getByRole("radio", { name: t("verdict.q.na") }));
    await userEvent.click(save);
    await waitFor(() => expect(onVerdict).toHaveBeenCalledTimes(1));
    const body = calls.find((c) => c.path.endsWith("/answers"))?.body as { answers: unknown[] };
    expect(body.answers).toEqual([
      { key: "survivorship_free", value: "na" },
      { key: "holdout_untouched", value: "no", note: "no holdout on purpose" },
    ]);
  });

  it("the suggestion can be overridden before saving", async () => {
    const { calls } = setup();
    await screen.findByText(new RegExp(t("verdict.question.survivorship_free")));
    const gaps = document.querySelector('[data-question="gaps_checked"]') as HTMLElement;
    await userEvent.click(within(gaps).getByRole("radio", { name: t("verdict.q.no") }));
    await userEvent.click(screen.getByRole("button", { name: t("verdict.q.save") }));
    await waitFor(() => expect(calls.some((c) => c.path.endsWith("/answers"))).toBe(true));
    expect((calls.find((c) => c.path.endsWith("/answers"))?.body as { answers: unknown[] }).answers).toEqual([{ key: "gaps_checked", value: "no" }]);
  });

  it("Change opens an answered question again", async () => {
    setup();
    await screen.findByText(new RegExp(t("verdict.question.survivorship_free")));
    expect(document.querySelector('[data-question="point_in_time"]')).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: t("verdict.q.change") }));
    expect(document.querySelector('[data-question="point_in_time"]')).not.toBeNull();
  });

  it("without the owner confirmation nothing can be answered", async () => {
    setup({ ownerBlocked: true });
    await screen.findByText(new RegExp(t("verdict.question.survivorship_free")));
    expect(screen.getByRole("button", { name: t("verdict.q.confirm") })).toBeDisabled();
    expect(screen.getByRole("button", { name: t("verdict.q.save") })).toBeDisabled();
    for (const radio of screen.getAllByRole("radio")) expect(radio).toBeDisabled();
  });

  it("Confirm suggestions is off when no question has a suggestion", async () => {
    mockApi({ "GET /api/verdict/ema_cross_nq/3/questions": QUESTIONS.map((q) => ({ ...q, suggestion: null })) });
    renderWithApp(<QuestionsSheet strategy="ema_cross_nq" version={3} verdict={makeVerdict()} ownerBlocked={false} onVerdict={() => undefined} onClose={() => undefined} />);
    await screen.findByText(new RegExp(t("verdict.question.survivorship_free")));
    expect(screen.getByRole("button", { name: t("verdict.q.confirm") })).toBeDisabled();
  });
});
