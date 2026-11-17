import { useState } from "react";
import { Button, Field, Select } from "@/design";
import { useI18n } from "@/i18n";
import { enginesApi, type Connection } from "./api";

const TIERS = ["fast", "balanced", "deep"] as const;
const AUTO = "auto";

/** A level is the student's when the map has no automatic mark (all of them) or names it in `pinned`. */
function pinnedLevels(saved: Record<string, string>): Set<string> {
  if (Object.keys(saved).length === 0) return new Set();
  if (!(AUTO in saved)) return new Set(TIERS);
  return new Set((saved.pinned ?? "").split(",").filter(Boolean));
}

/** Bug 1173: the models of a key connection come from its provider's list. Each level is "Automatic" (Hokma picks
 *  from the list and replaces a model that disappears) or a model the student chose from that same list. */
export function ModelPicker({ conn, onSaved }: { conn: Connection; onSaved: () => void }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [models, setModels] = useState<string[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const saved = conn.tier_models ?? {};
  const fixed = pinnedLevels(saved);
  const [choice, setChoice] = useState<Record<string, string>>(() =>
    Object.fromEntries(TIERS.map((tier) => [tier, fixed.has(tier) ? saved[tier] ?? AUTO : AUTO])),
  );

  const show = async () => {
    setOpen(true);
    setFailed(false);
    try {
      setModels((await enginesApi.models(conn.id)).models);
    } catch {
      setFailed(true);
      setModels([]);
    }
  };

  const save = async () => {
    setBusy(true);
    try {
      await enginesApi.setTiers(conn.id, choice);          // "auto" levels: the server keeps them automatic
      setOpen(false);
      onSaved();
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <div>
        <Button size="sm" variant="ghost" onClick={() => void show()}>
          {t("eng.models.open")}
        </Button>
      </div>
    );
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }} data-testid="model-picker">
      <p className="p" style={{ fontSize: 12 }}>{t("eng.models.lead")}</p>
      {failed ? <p className="p" role="status" style={{ fontSize: 12 }}>{t("eng.models.failed")}</p> : null}
      {models === null ? null : (
        <>
          {TIERS.map((tier) => {
            const current = choice[tier] ?? AUTO;
            const options = current !== AUTO && !models.includes(current) ? [current, ...models] : models;
            return (
              <Field key={tier} label={t(`eng.tier.${tier}`)}>
                {(control) => (
                  <Select {...control} value={current} onChange={(e) => setChoice({ ...choice, [tier]: e.target.value })}>
                    <option value={AUTO}>
                      {!fixed.has(tier) && saved[tier] ? t("eng.models.autoNow", { model: saved[tier] }) : t("eng.models.auto")}
                    </option>
                    {options.map((m) => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
            );
          })}
          <div style={{ display: "flex", gap: 8 }}>
            <Button size="sm" variant="primary" disabled={busy} onClick={() => void save()}>
              {t("eng.models.save")}
            </Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => setOpen(false)}>
              {t("eng.models.cancel")}
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
