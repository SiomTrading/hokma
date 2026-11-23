import { useEffect, useMemo, useState } from "react";
import { ApiError } from "@/api";
import { Button, Chip, cx, ErrorState, Field, Input, Loading, Modal, Option, Textarea, Toggle } from "@/design";
import { useI18n } from "@/i18n";
import { desksApi } from "./api";
import { formatTokens } from "./format";
import { parseDecimal, parseWhole } from "./parse";
import { DESK_LIMITS } from "./types";
import type { CreateDeskPayload, DeskTemplateDef, WizardTemplate } from "./types";

interface Props {
  onClose: () => void;
  /** Called with the id of the desk the server created and whether it was born with only the Yahoo fallback. */
  onCreated: (id: number, fallbackOnly: boolean) => void;
}

const STEPS = ["focus", "team", "rules", "assets"] as const;
const TEMPLATES: readonly WizardTemplate[] = ["minimal", "standard", "debate"];
const SPEND: Record<WizardTemplate, "ok" | "warn" | "bad"> = { minimal: "ok", standard: "warn", debate: "bad" };

/** The four-step assistant: focus, team of agents, rules (token budget) and assets. Creates a desk. */
export function TemplateWizard({ onClose, onCreated }: Props) {
  const { t, locale } = useI18n();
  const [step, setStep] = useState(0);
  const [name, setName] = useState("");
  const [focus, setFocus] = useState("");
  const [capital, setCapital] = useState("10000");
  const [objective, setObjective] = useState("");
  const [mandate, setMandate] = useState("");
  const [template, setTemplate] = useState<WizardTemplate>("standard");
  const [noCap, setNoCap] = useState(false);
  const [budgetText, setBudgetText] = useState<string | null>(null);
  const [symbol, setSymbol] = useState("");
  const [assets, setAssets] = useState<string[]>([]);
  const [templates, setTemplates] = useState<DeskTemplateDef[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [showGaps, setShowGaps] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const loadTemplates = () => {
    setLoadError(null);
    desksApi
      .templates()
      .then(setTemplates)
      .catch((cause: unknown) => setLoadError(cause instanceof ApiError ? cause.message : t("desks.error.templates")));
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(loadTemplates, []);

  const def = templates?.find((item) => item.id === template);
  const suggested = def?.token_budget ?? null;
  const capitalValue = parseWhole(capital);
  const objectiveValue = parseDecimal(objective);
  const budgetValue = budgetText === null ? suggested : parseWhole(budgetText);

  const gaps = useMemo(
    () => ({
      name: name.trim().length < 1 || name.trim().length > DESK_LIMITS.nameMax,
      capital: capitalValue === null || capitalValue < DESK_LIMITS.capitalMin || capitalValue > DESK_LIMITS.capitalMax,
      objective: objective.trim() !== "" && (objectiveValue === null || objectiveValue < 0 || objectiveValue > 100),
      budget: !noCap && budgetValue !== null && budgetValue < 1,
    }),
    [name, capitalValue, objective, objectiveValue, noCap, budgetValue],
  );
  const firstStepOk = !gaps.name && !gaps.capital && !gaps.objective;
  const canCreate = firstStepOk && !gaps.budget && def !== undefined && !busy;

  const addSymbol = () => {
    const clean = symbol.trim().toUpperCase().slice(0, DESK_LIMITS.symbolMax);
    if (!clean || assets.includes(clean) || assets.length >= DESK_LIMITS.assetsMax) return;
    setAssets([...assets, clean]);
    setSymbol("");
  };

  const next = () => {
    if (step === 0 && !firstStepOk) {
      setShowGaps(true);
      return;
    }
    setStep(Math.min(step + 1, STEPS.length - 1));
  };

  const create = async () => {
    if (!firstStepOk) {
      setShowGaps(true);
      setStep(0);
      return;
    }
    if (!canCreate || capitalValue === null) return;
    const payload: CreateDeskPayload = {
      name: name.trim(),
      focus: focus.trim() || null,
      capital: capitalValue,
      template,
      assets,
    };
    if (objectiveValue !== null) payload.objective_pct = objectiveValue;
    if (mandate.trim()) payload.mandate = mandate.trim();
    if (!noCap && budgetValue !== null) payload.token_budget = budgetValue;
    setBusy(true);
    setError(null);
    try {
      const created = await desksApi.create(payload);
      onCreated(created.id, created.connections?.fallback_only === true);
    } catch (cause) {
      setError(cause instanceof ApiError || cause instanceof Error ? cause.message : t("api.error"));
      setBusy(false);
    }
  };

  const required = t("desks.wizard.required");
  const last = step === STEPS.length - 1;
  return (
    <Modal label={t("desks.wizard.title")} onClose={onClose} style={{ width: "min(860px, 96vw)" }}>
      <nav className="snav" aria-label={t("desks.wizard.title")}>
        <span className="lbl">{t("desks.wizard.title")}</span>
        {STEPS.map((id, index) => (
          <button
            key={id}
            type="button"
            className={cx("step", "desk-step", index === step && "on", index < step && "done")}
            aria-current={index === step ? "step" : undefined}
            disabled={index > step && !firstStepOk}
            onClick={() => setStep(index)}
          >
            <i aria-hidden="true">{index < step ? "✓" : String(index + 1)}</i>
            <span className="hide-c">{t(`desks.wizard.step.${id}`)}</span>
          </button>
        ))}
        <p className="desk-wizard-note hide-c">{t("desks.wizard.simNote")}</p>
      </nav>
      <div className="desk-wizard-body">
        <h2 className="h1">{t(`desks.wizard.${STEPS[step]}.title`)}</h2>
        <p className="desk-wizard-lead">{t(`desks.wizard.${STEPS[step]}.lead`)}</p>

        {step === 0 ? (
          <div className="desk-wizard-form">
            <Field label={t("desks.field.name")} error={showGaps && gaps.name ? required : undefined}>
              {(control) => <Input {...control} value={name} maxLength={DESK_LIMITS.nameMax} autoFocus onChange={(e) => setName(e.target.value)} />}
            </Field>
            <Field label={t("desks.field.focus")} hint={t("desks.field.focus.hint")}>
              {(control) => <Input {...control} value={focus} maxLength={DESK_LIMITS.focusMax} onChange={(e) => setFocus(e.target.value)} />}
            </Field>
            <div className="desk-wizard-two">
              <Field label={t("desks.field.capital")} hint={t("desks.field.capital.hint")} error={showGaps && gaps.capital ? t("desks.wizard.capitalRange", { min: formatTokens(DESK_LIMITS.capitalMin, locale) }) : undefined}>
                {(control) => <Input {...control} className="mono" inputMode="numeric" value={capital} onChange={(e) => setCapital(e.target.value)} />}
              </Field>
              <Field label={t("desks.field.objective")} hint={t("desks.field.objective.hint")} error={showGaps && gaps.objective ? t("desks.wizard.objectiveRange") : undefined}>
                {(control) => <Input {...control} className="mono" inputMode="decimal" value={objective} onChange={(e) => setObjective(e.target.value)} />}
              </Field>
            </div>
            <Field label={t("desks.field.mandate")} hint={t("desks.field.mandate.hint")}>
              {(control) => <Textarea {...control} rows={4} value={mandate} maxLength={DESK_LIMITS.mandateMax} onChange={(e) => setMandate(e.target.value)} />}
            </Field>
          </div>
        ) : null}

        {step === 1 ? (
          <div className="desk-wizard-form">
            <div className="desk-tpl-grid" role="radiogroup" aria-label={t("desks.wizard.team.title")}>
              {TEMPLATES.map((id) => (
                <Option key={id} selected={template === id} onSelect={() => setTemplate(id)}>
                  <b>{t(`desks.tpl.${id}.name`)}</b>
                  <span className="desk-tpl-text">{t(`desks.tpl.${id}.text`)}</span>
                  <span className="mono desk-tpl-spend" style={{ color: `var(--${SPEND[id]})` }}>
                    {t(`desks.tpl.${id}.spend`)}
                  </span>
                </Option>
              ))}
            </div>
            {template === "debate" ? <p className="hint">{t("desks.tpl.debate.horizon")}</p> : null}
            {loadError ? (
              <ErrorState message={loadError} onRetry={loadTemplates} />
            ) : templates === null ? (
              <Loading inline label={t("desks.wizard.loadingTemplates")} />
            ) : (
              <div className="pane desk-chips" aria-label={t("desks.wizard.chips")}>
                {(def?.agents ?? []).map((agent) => (
                  <Chip key={`${agent.slug}-${agent.role}`}>
                    {t(`desks.role.${agent.role}`)} · {t(`desks.model.${agent.model}`)}
                  </Chip>
                ))}
              </div>
            )}
            <p className="hint">{t("desks.wizard.team.after")}</p>
          </div>
        ) : null}

        {step === 2 ? (
          <div className="desk-wizard-form">
            <div className="desk-budget-row">
              <Toggle checked={!noCap} onChange={(on) => setNoCap(!on)} label={t("desks.budget.limit")} />
              <span>{t("desks.budget.limit")}</span>
            </div>
            {!noCap ? (
              <Field
                label={t("desks.budget.weekly")}
                hint={suggested !== null ? t("desks.budget.suggested", { n: formatTokens(suggested, locale) }) : undefined}
                error={gaps.budget ? t("desks.budget.invalid") : undefined}
              >
                {(control) => (
                  <Input
                    {...control}
                    className="mono"
                    inputMode="numeric"
                    value={budgetText ?? (suggested !== null ? formatTokens(suggested, locale) : "")}
                    onChange={(e) => setBudgetText(e.target.value)}
                  />
                )}
              </Field>
            ) : (
              <p className="hint">{t("desks.budget.nocap")}</p>
            )}
            <p className="hint">{t("desks.budget.how")}</p>
            <div className="pane desk-later">
              <b>{t("desks.rules.title")}</b>
              <span>{t("desks.rules.text")}</span>
            </div>
          </div>
        ) : null}

        {step === 3 ? (
          <div className="desk-wizard-form">
            <div className="desk-asset-add">
              <Field label={t("desks.field.asset")} hint={t("desks.field.asset.hint")}>
                {(control) => (
                  <Input
                    {...control}
                    className="mono"
                    value={symbol}
                    maxLength={DESK_LIMITS.symbolMax}
                    onChange={(e) => setSymbol(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        addSymbol();
                      }
                    }}
                  />
                )}
              </Field>
              <Button onClick={addSymbol} disabled={!symbol.trim() || assets.length >= DESK_LIMITS.assetsMax}>
                {t("desks.field.asset.add")}
              </Button>
            </div>
            <div className="desk-chips" aria-label={t("desks.wizard.assetsList")}>
              {assets.length === 0 ? <span className="hint">{t("desks.wizard.assetsEmpty")}</span> : null}
              {assets.map((item) => (
                <Chip key={item}>
                  <span className="mono">{item}</span>
                  <button type="button" className="ib" aria-label={t("desks.field.asset.remove", { symbol: item })} onClick={() => setAssets(assets.filter((x) => x !== item))}>
                    ✕
                  </button>
                </Chip>
              ))}
            </div>
            <p className="hint">{t("desks.wizard.assetsData")}</p>
          </div>
        ) : null}

        {error ? <p className="err" role="alert">{error}</p> : null}
        <div className="desk-wizard-actions">
          <Button variant="ghost" onClick={step === 0 ? onClose : () => setStep(step - 1)}>
            {t(step === 0 ? "desks.wizard.cancel" : "desks.wizard.back")}
          </Button>
          <span className="desk-wizard-right">
            {step > 0 && !last ? (
              <Button onClick={() => void create()} disabled={!canCreate}>
                {t("desks.wizard.create")}
              </Button>
            ) : null}
            {last ? (
              <Button variant="primary" onClick={() => void create()} disabled={!canCreate}>
                {t("desks.wizard.create")}
              </Button>
            ) : (
              <Button variant="primary" onClick={next}>
                {t(`desks.wizard.next.${STEPS[step + 1]}`)}
              </Button>
            )}
          </span>
        </div>
      </div>
    </Modal>
  );
}
