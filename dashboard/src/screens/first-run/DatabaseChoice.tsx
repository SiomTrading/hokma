import { useState } from "react";
import { ApiError } from "@/api";
import { Button, Option, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { setupApi } from "./api";
import type { DbChoice } from "./types";

interface Props {
  engine: DbChoice;
  onEngine: (engine: DbChoice) => void;
  url: string;
  onUrl: (url: string) => void;
  /** True once the typed address passed "Test connection". */
  tested: boolean;
  onTested: (ok: boolean) => void;
}

export function DatabaseChoice({ engine, onEngine, url, onUrl, tested, onTested }: Props) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const test = async () => {
    setBusy(true);
    setMessage(null);
    try {
      const result = await setupApi.testDatabase(url);
      onTested(result.ok);
      setMessage(result.ok ? t("firstrun.db.testOk") : result.message || t("firstrun.db.testFailed"));
    } catch (cause) {
      onTested(false);
      setMessage(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div role="radiogroup" aria-label={t("firstrun.db.title")} style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))", gap: 10 }}>
        <Option selected={engine === "sqlite"} onSelect={() => onEngine("sqlite")}>
          <span style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
            <span className="h2">{t("firstrun.db.local")}</span>
            <Pill tone="ok">{t("firstrun.db.localFree")}</Pill>
          </span>
          <span className="p">{t("firstrun.db.localBody")}</span>
        </Option>
        <Option selected={engine === "postgres"} onSelect={() => onEngine("postgres")}>
          <span style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
            <span className="h2">{t("firstrun.db.pg")}</span>
            <Pill>{t("firstrun.db.pgTag")}</Pill>
          </span>
          <span className="p">{t("firstrun.db.pgBody")}</span>
        </Option>
      </div>
      {engine === "postgres" ? (
        <div className="fade" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <label htmlFor="hk-pg-url" className="lbl">
            {t("firstrun.db.url")}
          </label>
          <input
            id="hk-pg-url"
            className="in mono"
            style={{ fontSize: 12 }}
            placeholder={t("firstrun.db.urlPlaceholder")}
            value={url}
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => {
              onUrl(e.target.value);
              onTested(false);
              setMessage(null);
            }}
          />
          <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
            <Button size="sm" disabled={busy || !url.trim()} onClick={() => void test()}>
              {busy ? t("firstrun.db.testing") : t("firstrun.db.test")}
            </Button>
            {message ? (
              <Pill tone={tested ? "ok" : "bad"} role="status">
                {message}
              </Pill>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
