import { useState } from "react";
import { Button, Chip, Field, Input } from "@/design";
import { useI18n } from "@/i18n";
import { deskConfigApi, failText } from "../../configApi";
import type { DeskDetail } from "../../configTypes";
import { DESK_LIMITS } from "../../types";

interface Props {
  deskId: number;
  detail: DeskDetail;
  onSaved: (detail: DeskDetail) => void;
}

/**
 * Assets the desk watches. Every add or remove sends the whole list to the server, which checks each symbol against
 * the market catalog. If the server refuses, the list stays as it was and the reason is shown.
 */
export function AssetsSection({ deskId, detail, onSaved }: Props) {
  const { t } = useI18n();
  const [symbol, setSymbol] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const assets = detail.assets;

  const write = async (next: string[]): Promise<boolean> => {
    setBusy(true);
    setError(null);
    try {
      await deskConfigApi.setAssets(deskId, next);
      onSaved({ ...detail, assets: next });
      return true;
    } catch (cause) {
      setError(failText(cause, t("desks.cfg.error.assets")));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const add = async () => {
    const clean = symbol.trim().toUpperCase().slice(0, DESK_LIMITS.symbolMax);
    if (!clean || busy) return;
    if (assets.includes(clean)) {
      setError(t("desks.cfg.assets.duplicate", { symbol: clean }));
      return;
    }
    if (assets.length >= DESK_LIMITS.assetsMax) {
      setError(t("desks.cfg.assets.max", { n: String(DESK_LIMITS.assetsMax) }));
      return;
    }
    if (await write([...assets, clean])) setSymbol("");
  };

  return (
    <div className="desk-cfg-form">
      <div className="lbl">{t("desks.cfg.assets.title")}</div>
      <div className="desk-chips desk-cfg-chips" aria-label={t("desks.wizard.assetsList")}>
        {assets.length === 0 ? <span className="hint">{t("desks.cfg.assets.empty")}</span> : null}
        {assets.map((item) => (
          <Chip key={item}>
            <span className="mono">{item}</span>
            <button type="button" className="ib" disabled={busy} aria-label={t("desks.field.asset.remove", { symbol: item })} onClick={() => void write(assets.filter((x) => x !== item))}>
              ✕
            </button>
          </Chip>
        ))}
      </div>
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
                  void add();
                }
              }}
            />
          )}
        </Field>
        <Button onClick={() => void add()} disabled={!symbol.trim() || busy}>
          {t("desks.field.asset.add")}
        </Button>
      </div>
      {error ? (
        <p className="err" role="alert">
          {error}
        </p>
      ) : null}
      <p className="hint desk-cfg-note">{t("desks.cfg.assets.note")}</p>
    </div>
  );
}
