import { useState } from "react";
import { Button, Field, Input, Textarea } from "@/design";
import { useI18n } from "@/i18n";
import { deskConfigApi, failText } from "../../configApi";
import type { DeskDetail, DeskPatch } from "../../configTypes";
import { parseDecimal, parseWhole } from "../../parse";
import { refreshDesks } from "../../store";
import { formatTokens } from "../../format";
import { DESK_LIMITS } from "../../types";

interface Props {
  deskId: number;
  detail: DeskDetail;
  onSaved: (detail: DeskDetail) => void;
}

/** Name, focus, capital, monthly goal and mandate. One Save button writes only what changed. */
export function ObjectiveSection({ deskId, detail, onSaved }: Props) {
  const { t, locale } = useI18n();
  const [name, setName] = useState(detail.name);
  const [focus, setFocus] = useState(detail.focus ?? "");
  const [capital, setCapital] = useState(String(detail.capital));
  const [objective, setObjective] = useState(detail.objective_pct === null ? "" : String(detail.objective_pct).replace(".", locale === "es" ? "," : "."));
  const [mandate, setMandate] = useState(detail.mandate ?? "");
  const [budget, setBudget] = useState(detail.token_budget === null ? "" : formatTokens(detail.token_budget, locale));
  const [showGaps, setShowGaps] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const capitalValue = parseWhole(capital);
  const objectiveValue = parseDecimal(objective);
  const budgetValue = budget.trim() === "" ? null : parseWhole(budget);
  const gaps = {
    name: name.trim().length < 1 || name.trim().length > DESK_LIMITS.nameMax,
    capital: capitalValue === null || capitalValue < DESK_LIMITS.capitalMin || capitalValue > DESK_LIMITS.capitalMax,
    objective: objective.trim() !== "" && (objectiveValue === null || objectiveValue < 0 || objectiveValue > 100),
    budget: budget.trim() !== "" && (budgetValue === null || budgetValue < 1),
  };

  const touch = (set: (value: string) => void) => (value: string) => {
    set(value);
    setSaved(false);
  };

  const patch: DeskPatch = {};
  if (name.trim() !== detail.name) patch.name = name.trim();
  if ((focus.trim() || null) !== detail.focus) patch.focus = focus.trim() || null;
  if (capitalValue !== null && capitalValue !== detail.capital) patch.capital = capitalValue;
  if (objectiveValue !== detail.objective_pct && !gaps.objective) patch.objective_pct = objectiveValue;
  if ((mandate.trim() || null) !== detail.mandate) patch.mandate = mandate.trim() || null;
  if (budgetValue !== detail.token_budget && !gaps.budget) patch.token_budget = budgetValue;
  const dirty = Object.keys(patch).length > 0;

  const save = async () => {
    if (gaps.name || gaps.capital || gaps.objective || gaps.budget) {
      setShowGaps(true);
      return;
    }
    if (!dirty || busy) return;
    setBusy(true);
    setError(null);
    try {
      await deskConfigApi.update(deskId, patch);
      onSaved({
        ...detail,
        name: name.trim(),
        focus: focus.trim() || null,
        capital: capitalValue ?? detail.capital,
        objective_pct: objectiveValue,
        mandate: mandate.trim() || null,
        token_budget: budgetValue,
      });
      setSaved(true);
      void refreshDesks();
    } catch (cause) {
      setError(failText(cause, t("desks.cfg.error.save")));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="desk-cfg-form"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <div className="lbl">{t("desks.cfg.section.objective")}</div>
      <div className="desk-cfg-grid">
        <Field label={t("desks.field.name")} error={showGaps && gaps.name ? t("desks.wizard.required") : undefined}>
          {(control) => <Input {...control} value={name} maxLength={DESK_LIMITS.nameMax} onChange={(e) => touch(setName)(e.target.value)} />}
        </Field>
        <Field label={t("desks.field.focus")}>
          {(control) => <Input {...control} value={focus} maxLength={DESK_LIMITS.focusMax} onChange={(e) => touch(setFocus)(e.target.value)} />}
        </Field>
        <Field
          label={t("desks.field.capital")}
          hint={t("desks.field.capital.hint")}
          error={showGaps && gaps.capital ? t("desks.wizard.capitalRange", { min: DESK_LIMITS.capitalMin.toLocaleString(locale) }) : undefined}
        >
          {(control) => <Input {...control} className="mono" inputMode="numeric" value={capital} onChange={(e) => touch(setCapital)(e.target.value)} />}
        </Field>
        <Field label={t("desks.cfg.objective")} error={showGaps && gaps.objective ? t("desks.wizard.objectiveRange") : undefined}>
          {(control) => <Input {...control} className="mono" inputMode="decimal" value={objective} onChange={(e) => touch(setObjective)(e.target.value)} />}
        </Field>
        <Field label={t("desks.cfg.budget")} hint={t("desks.cfg.budget.hint")} error={showGaps && gaps.budget ? t("desks.budget.invalid") : undefined}>
          {(control) => <Input {...control} className="mono" inputMode="numeric" value={budget} onChange={(e) => touch(setBudget)(e.target.value)} />}
        </Field>
        <div className="desk-cfg-wide">
          <Field label={t("desks.cfg.mandate")} hint={t("desks.cfg.mandate.hint")}>
            {(control) => <Textarea {...control} rows={5} value={mandate} maxLength={DESK_LIMITS.mandateMax} onChange={(e) => touch(setMandate)(e.target.value)} />}
          </Field>
        </div>
      </div>
      {error ? (
        <p className="err" role="alert">
          {error}
        </p>
      ) : null}
      <div className="desk-cfg-actions">
        <Button type="submit" variant="primary" disabled={!dirty || busy}>
          {t("desks.cfg.save")}
        </Button>
        {saved ? (
          <span className="hint" role="status">
            {t("desks.cfg.saved")}
          </span>
        ) : null}
      </div>
    </form>
  );
}
