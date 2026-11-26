import { useState } from "react";
import { useApiQuery } from "@/api";
import { Select } from "@/design";
import { useI18n } from "@/i18n";
import { paths } from "./api";
import { Block, Empty, QueryView } from "./Block";
import { describeParams, formatNumber, svgDataUrl, toNumber } from "./format";
import type { HeatmapData, HeatmapResponse, Ideal } from "./types";

/** Geometry of Heatmap.svg() in backend/hokma/backtest/surface.py (cell 48, left 76, top 28, gap 8, 38 under the cells). */
const DEFAULT_LAYOUT = { cell: 48, left: 76, top: 28, gap: 8, bottom: 38 };

type Layout = typeof DEFAULT_LAYOUT;

function nearestIndex(values: (number | string)[], wanted: number | string | undefined): number | null {
  if (wanted === undefined) return null;
  const exact = values.findIndex((value) => value === wanted);
  if (exact >= 0) return exact;
  const target = toNumber(wanted);
  if (target === null) return null;
  let best: number | null = null;
  let bestGap = Infinity;
  values.forEach((value, index) => {
    const n = toNumber(value);
    if (n === null) return;
    const gap = Math.abs(n - target);
    if (gap < bestGap) {
      best = index;
      bestGap = gap;
    }
  });
  return best;
}

/** Center of the cell of a point on the picture, in pixels; null when the point has no value on one of the two axes. */
export function markerPosition(
  map: HeatmapData,
  layout: Layout,
  params: Record<string, number | string> | null | undefined,
): { left: number; top: number } | null {
  if (!params) return null;
  const ix = nearestIndex(map.x_values, params[map.x]);
  const iy = nearestIndex(map.y_values, params[map.y]);
  if (ix === null || iy === null) return null;
  const rows = map.y_values.length;
  return { left: layout.left + ix * layout.cell + layout.cell / 2, top: layout.top + (rows - 1 - iy) * layout.cell + layout.cell / 2 };
}

interface Props {
  /** The optimization run of the verdict (null when it has none). */
  optId: number | null;
  ideal: Ideal;
}

/** E: the heat map (an image, never HTML), the axis selectors and the markers of the ideal result and of the best point. */
export function OptimizationBlock({ optId, ideal }: Props) {
  const { t, locale } = useI18n();
  const [x, setX] = useState<string | undefined>(undefined);
  const [y, setY] = useState<string | undefined>(undefined);
  const query = useApiQuery<HeatmapResponse>(optId === null ? null : paths.heatmap(optId, x, y));
  const empty = <Empty>{t("verdict.opt.empty")}</Empty>;

  return (
    <Block title={t("verdict.block.optimization")} name="optimization">
      {optId === null ? (
        <>
          {empty}
          <IdealText ideal={ideal} locale={locale} />
        </>
      ) : (
        <QueryView state={query} empty={empty}>
          {(data) => {
            const map = data.heatmap;
            const layout = { ...DEFAULT_LAYOUT, ...(data.layout ?? {}) };
            const width = layout.left + map.x_values.length * layout.cell + layout.gap;
            const height = layout.top + map.y_values.length * layout.cell + layout.bottom;
            const idealAt = ideal.status === "found" ? markerPosition(map, layout, ideal.params) : null;
            const bestAt = markerPosition(map, layout, data.best?.params);
            const names = data.axes.map((axis) => axis.name);
            const fixed = Object.entries(map.fixed)
              .map(([name, value]) => `${name} = ${typeof value === "number" ? value.toLocaleString(locale, { maximumFractionDigits: 6 }) : value}`)
              .join(", ");
            const bestEdge = data.best !== null && data.edge_of_space.some((name) => data.best?.params[name] !== undefined);
            return (
              <>
                <div className="bt-opt-controls">
                  <label className="bt-opt-select">
                    <span className="lbl">{t("verdict.opt.axisX")}</span>
                    <Select value={map.x} onChange={(event) => setX(event.target.value)}>
                      {names.map((name) => (
                        <option key={name} value={name} disabled={name === map.y}>
                          {name}
                        </option>
                      ))}
                    </Select>
                  </label>
                  <label className="bt-opt-select">
                    <span className="lbl">{t("verdict.opt.axisY")}</span>
                    <Select value={map.y} onChange={(event) => setY(event.target.value)}>
                      {names.map((name) => (
                        <option key={name} value={name} disabled={name === map.x}>
                          {name}
                        </option>
                      ))}
                    </Select>
                  </label>
                  <label className="bt-opt-select">
                    <span className="lbl">{t("verdict.opt.metric")}</span>
                    <Select value={map.metric} onChange={() => undefined}>
                      <option value={map.metric}>{map.metric}</option>
                    </Select>
                  </label>
                  {fixed ? <span className="p">({t("verdict.opt.fixed", { list: fixed })})</span> : null}
                </div>
                <div className="bt-opt-body">
                  <div className="bt-heat" style={{ width, height }}>
                    <img
                      src={svgDataUrl(data.svg)}
                      width={width}
                      height={height}
                      alt={t("verdict.opt.alt", { metric: map.metric, x: map.x, y: map.y })}
                    />
                    {idealAt ? <span className="bt-marker bt-marker-ideal" data-marker="ideal" aria-hidden="true" style={{ left: idealAt.left, top: idealAt.top }} /> : null}
                    {bestAt ? <span className="bt-marker bt-marker-best" data-marker="best" aria-hidden="true" style={{ left: bestAt.left, top: bestAt.top }} /> : null}
                  </div>
                  <div className="bt-opt-side">
                    <IdealText ideal={ideal} locale={locale} />
                    {data.best ? (
                      <p className="p">{t(bestEdge ? "verdict.opt.bestEdge" : "verdict.opt.best", { params: describeParams(data.best.params, locale) })}</p>
                    ) : null}
                    <ul className="bt-legend">
                      <li>
                        <span className="bt-marker bt-marker-ideal bt-marker-key" aria-hidden="true" /> {t("verdict.opt.legendIdeal")}
                      </li>
                      <li>
                        <span className="bt-marker bt-marker-best bt-marker-key" aria-hidden="true" /> {t("verdict.opt.legendBest")}
                      </li>
                    </ul>
                  </div>
                </div>
                <p className="p bt-opt-counts">
                  {t("verdict.opt.counts", {
                    n: data.n_trials.toLocaleString(locale),
                    failed: data.failed.toLocaleString(locale),
                    cached: data.cache_hits.toLocaleString(locale),
                  })}
                  {data.warnings.map((warning) => ` | ${t("verdict.opt.warning", { warning })}`).join("")}
                </p>
              </>
            );
          }}
        </QueryView>
      )}
    </Block>
  );
}

function IdealText({ ideal, locale }: { ideal: Ideal; locale: string }) {
  const { t } = useI18n();
  if (ideal.status === "not_found") {
    const code = ideal.reason ?? "";
    const text = t(`verdict.ideal.reason.${code}`);
    const reason = code === "" ? "" : text === `verdict.ideal.reason.${code}` ? code : text;
    return <p className="p bt-ideal">{t("verdict.opt.notFound", { reason })}</p>;
  }
  const mean = formatNumber(toNumber(ideal.plateau_mean), locale);
  return (
    <div className="bt-ideal">
      <p className="p">
        <b>{t("verdict.opt.ideal", { params: describeParams(ideal.params, locale) })}</b>
      </p>
      {ideal.plateau_size !== null ? (
        <p className="p">
          {mean ? t("verdict.opt.plateauMean", { n: ideal.plateau_size, mean }) : t("verdict.opt.plateau", { n: ideal.plateau_size })}
        </p>
      ) : null}
      <p className="p">{t("verdict.opt.passes")}</p>
    </div>
  );
}
