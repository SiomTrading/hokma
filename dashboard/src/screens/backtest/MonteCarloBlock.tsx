import { useState } from "react";
import type { QueryState } from "@/api";
import { Button } from "@/design";
import { useI18n } from "@/i18n";
import { errorText, verdictApi } from "./api";
import { Block, Empty, QueryView } from "./Block";
import { formatNumber, formatShare } from "./format";
import type { MonteCarloData, Spread } from "./types";

interface Props {
  /** The run the Monte Carlo belongs to: the POST creates its child. */
  runId: number;
  /** The saved Monte Carlo (null when the verdict has none). */
  query: QueryState<MonteCarloData> | null;
  ownerBlocked: boolean;
}

function isSpread(value: unknown): value is Spread {
  return typeof value === "object" && value !== null && "median" in value && "percentile_5" in value && "percentile_95" in value;
}

function Result({ data }: { data: MonteCarloData }) {
  const { t, locale } = useI18n();
  const isShare = (name: string) => name === "cagr" || name === "max_drawdown";
  const show = (name: string, value: number) => (isShare(name) ? formatShare(value, locale) : formatNumber(value, locale)) ?? "";
  const spreads = Object.entries(data.figures).filter((entry): entry is [string, Spread] => isSpread(entry[1]));
  const label = (name: string) => {
    const text = t(`verdict.mc.figure.${name}`);
    return text === `verdict.mc.figure.${name}` ? name : text;
  };
  return (
    <>
      <p className="p">{t("verdict.mc.method", { method: data.method, n: data.n.toLocaleString(locale), seed: data.seed })}</p>
      <ul className="bt-mc">
        {spreads.map(([name, spread]) => (
          <li key={name} className="p">
            <b>{label(name)}</b>: {t("verdict.mc.worst")} {show(name, spread.percentile_5)} | {t("verdict.mc.median")} {show(name, spread.median)} |{" "}
            {t("verdict.mc.best")} {show(name, spread.percentile_95)}
          </li>
        ))}
      </ul>
      {data.p_value !== null ? (
        <p className="p">{t("verdict.mc.placebo", { pct: formatShare(data.p_value, locale, 0) ?? "", p: formatNumber(data.p_value, locale) ?? "" })}</p>
      ) : null}
      {data.warnings.length > 0 ? <p className="p bt-warnings">{data.warnings.join(", ")}</p> : null}
    </>
  );
}

/** G: the Monte Carlo that is saved, or the button that runs it with the defaults of H4 when it is missing. */
export function MonteCarloBlock({ runId, query, ownerBlocked }: Props) {
  const { t } = useI18n();
  const [fresh, setFresh] = useState<MonteCarloData | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    setRunning(true);
    setError(null);
    try {
      setFresh(await verdictApi.runMonteCarlo(runId));
    } catch (cause) {
      setError(errorText(cause, t("verdict.error.generic")));
    } finally {
      setRunning(false);
    }
  };

  const missing = (
    <div className="bt-mc-missing">
      <Empty>{t("verdict.mc.missing")}</Empty>
      <Button variant="primary" onClick={() => void run()} disabled={ownerBlocked || running}>
        {running ? t("verdict.mc.running") : t("verdict.mc.run")}
      </Button>
      {error ? (
        <p className="err" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );

  return (
    <Block title={t("verdict.block.montecarlo")} name="montecarlo">
      {fresh ? (
        <>
          <Result data={fresh} />
          <p className="p bt-hint">{t("verdict.mc.done")}</p>
        </>
      ) : query === null ? (
        missing
      ) : (
        <QueryView state={query} empty={missing}>
          {(data) => <Result data={data} />}
        </QueryView>
      )}
    </Block>
  );
}
