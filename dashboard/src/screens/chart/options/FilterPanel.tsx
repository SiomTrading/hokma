import { useState } from "react";
import { Button } from "@/design";
import { useI18n } from "@/i18n";
import { readNumber } from "./filter";
import type { FilterValues } from "./filter";

const FIELDS: ReadonlyArray<{ name: keyof FilterValues; label: string; step: string }> = [
  { name: "minDays", label: "options.filter.minDays", step: "1" },
  { name: "maxDays", label: "options.filter.maxDays", step: "1" },
  { name: "minDelta", label: "options.filter.minDelta", step: "0.01" },
  { name: "maxDelta", label: "options.filter.maxDelta", step: "0.01" },
  { name: "minOpenInterest", label: "options.filter.minOpenInterest", step: "1" },
  { name: "maxSpread", label: "options.filter.maxSpread", step: "0.1" },
];

interface Props {
  values: FilterValues;
  active: boolean;
  onApply: (values: FilterValues) => void;
  onClear: () => void;
}

/** The fields of the filter, with the PEAD rule filled in. Apply turns the filter on; Clear turns it off. */
export function FilterPanel({ values, active, onApply, onClear }: Props) {
  const { t } = useI18n();
  const [draft, setDraft] = useState<Record<keyof FilterValues, string>>(
    () => Object.fromEntries(FIELDS.map((f) => [f.name, String(values[f.name])])) as Record<keyof FilterValues, string>,
  );
  const parsed = Object.fromEntries(FIELDS.map((f) => [f.name, readNumber(draft[f.name])])) as Record<keyof FilterValues, number | null>;
  const valid = FIELDS.every((f) => parsed[f.name] !== null && parsed[f.name]! >= 0) && parsed.minDays! <= parsed.maxDays! && parsed.minDelta! <= parsed.maxDelta!;
  return (
    <form
      className="opt-filter"
      data-testid="options-filter-panel"
      onSubmit={(event) => {
        event.preventDefault();
        if (valid) onApply(parsed as FilterValues);
      }}
    >
      {FIELDS.map((field) => (
        <label key={field.name} className="opt-filter-field">
          <span>{t(field.label)}</span>
          <input
            className="in mono"
            type="number"
            inputMode="decimal"
            min="0"
            step={field.step}
            value={draft[field.name]}
            onChange={(event) => setDraft((d) => ({ ...d, [field.name]: event.target.value }))}
          />
        </label>
      ))}
      <div className="opt-filter-actions">
        <Button type="submit" size="sm" variant="primary" disabled={!valid}>
          {t("options.filter.apply")}
        </Button>
        <Button type="button" size="sm" disabled={!active} onClick={onClear}>
          {t("options.filter.clear")}
        </Button>
      </div>
    </form>
  );
}
