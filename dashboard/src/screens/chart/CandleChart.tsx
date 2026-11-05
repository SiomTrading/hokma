import { useMemo, useState } from "react";
import type { PointerEvent } from "react";
import { useI18n } from "@/i18n";
import { candleSpan, formatBarTime, formatPrice, formatSessionDate, formatVolume } from "./format";
import { VIEW_H, VIEW_W, candleIndexAt, layoutChart } from "./geometry";
import type { ChartData } from "./types";

const TIME_LABELS = 5;

/** Candlesticks drawn as plain SVG, stretched to its box like the design. Labels are HTML so they never distort. */
export function CandleChart({ data }: { data: ChartData }) {
  const { t, locale } = useI18n();
  const [hover, setHover] = useState<number | null>(null);
  const layout = useMemo(() => layoutChart(data.candles, data.opening_range), [data.candles, data.opening_range]);
  const span = candleSpan(data.candles);
  const count = data.candles.length;
  const pct = (y: number) => `${(y / VIEW_H) * 100}%`;
  const pctX = (x: number) => `${(x / VIEW_W) * 100}%`;

  const labelIndexes = useMemo(() => {
    if (count === 0) return [];
    const picks = new Set<number>();
    for (let i = 0; i < TIME_LABELS; i += 1) picks.add(Math.min(count - 1, Math.round(((i + 0.5) / TIME_LABELS) * (count - 1))));
    return [...picks];
  }, [count]);

  const onMove = (event: PointerEvent<HTMLDivElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    if (box.width === 0) return;
    const index = candleIndexAt(((event.clientX - box.left) / box.width) * VIEW_W, count);
    setHover(index < 0 ? null : index);
  };

  const hovered = hover !== null ? data.candles[hover] : undefined;
  const hoveredShape = hover !== null ? layout.shapes[hover] : undefined;
  const opening = data.opening_range;
  const openingY = layout.openingY;

  return (
    <div style={{ flex: 1, display: "flex", minHeight: 260 }}>
    <div
      data-testid="chart-body"
      style={{ flex: 1, minWidth: 0, position: "relative", touchAction: "pan-y" }}
      onPointerMove={onMove}
      onPointerLeave={() => setHover(null)}
    >
      <svg
        viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
        preserveAspectRatio="none"
        style={{ position: "absolute", inset: 0, width: "100%", height: "100%" }}
        role="img"
        aria-label={t("chart.aria", { symbol: data.symbol, tf: data.timeframe })}
      >
        {layout.grid.map((line) => (
          <line key={line.y} x1={0} x2={VIEW_W} y1={line.y} y2={line.y} stroke="var(--grid)" strokeWidth={1} vectorEffect="non-scaling-stroke" />
        ))}
        {layout.shapes.map((shape) => {
          const color = shape.up ? "var(--up)" : "var(--down)";
          return (
            <g key={data.candles[shape.index]!.t} data-candle={shape.up ? "up" : "down"}>
              <line x1={shape.cx} x2={shape.cx} y1={shape.wickTop} y2={shape.wickBottom} stroke={color} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
              <rect x={shape.x} y={shape.bodyY} width={shape.width} height={shape.bodyH} fill={color} rx={1.5} />
            </g>
          );
        })}
        {openingY !== null && opening ? (
          <line
            data-testid="opening-line"
            data-price={opening.high}
            x1={0}
            x2={VIEW_W}
            y1={openingY}
            y2={openingY}
            stroke="var(--org)"
            strokeWidth={1}
            strokeDasharray="6 6"
            vectorEffect="non-scaling-stroke"
          />
        ) : null}
        {hoveredShape ? (
          <line x1={hoveredShape.cx} x2={hoveredShape.cx} y1={0} y2={VIEW_H} stroke="var(--t3)" strokeWidth={1} strokeDasharray="2 4" vectorEffect="non-scaling-stroke" />
        ) : null}
      </svg>

      {labelIndexes.map((index) => (
        <span key={index} className="mono" aria-hidden="true" style={{ position: "absolute", bottom: 6, left: pctX(layout.shapes[index]!.cx), transform: "translateX(-50%)", fontSize: 10, color: "var(--t3)", pointerEvents: "none", whiteSpace: "nowrap" }}>
          {formatBarTime(data.candles[index]!.t, data.timeframe, data.timezone, locale, span)}
        </span>
      ))}

      {openingY !== null && opening ? (
        <span
          className="pill org"
          aria-label={t("chart.opening.aria", { date: formatSessionDate(opening.session_date, locale), price: formatPrice(opening.high, locale) })}
          style={{ position: "absolute", right: 12, backdropFilter: "blur(6px)", WebkitBackdropFilter: "blur(6px)", top: openingY / VIEW_H > 0.1 ? `calc(${pct(openingY)} - 26px)` : `calc(${pct(openingY)} + 6px)`, pointerEvents: "none" }}
        >
          {t("chart.opening")} <span className="mono">{formatPrice(opening.high, locale)}</span>
        </span>
      ) : null}

      {hovered ? (
        <div className="mono" data-testid="chart-readout" style={{ position: "absolute", top: 6, left: 8, right: 8, fontSize: 11, color: "var(--t2)", pointerEvents: "none", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
          {t("chart.hover", {
            time: formatBarTime(hovered.t, data.timeframe, data.timezone, locale, span),
            o: formatPrice(hovered.o, locale),
            h: formatPrice(hovered.h, locale),
            l: formatPrice(hovered.l, locale),
            c: formatPrice(hovered.c, locale),
            v: formatVolume(hovered.v, locale),
          })}
        </div>
      ) : null}
    </div>
    <div data-testid="price-axis" aria-hidden="true" style={{ width: 78, flexShrink: 0, position: "relative", borderLeft: "1px solid var(--border)" }}>
      {layout.grid.map((line) => (
        <span key={line.y} className="mono" style={{ position: "absolute", left: 8, top: pct(line.y), transform: "translateY(-50%)", fontSize: 10, color: "var(--t3)", whiteSpace: "nowrap" }}>
          {formatPrice(line.price, locale)}
        </span>
      ))}
    </div>
    </div>
  );
}
