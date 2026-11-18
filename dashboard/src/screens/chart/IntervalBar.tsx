import { useRef, useState } from "react";
import type { CSSProperties, RefObject } from "react";
import { Segmented } from "@/design";
import { useI18n } from "@/i18n";
import { loadFavorites, toggleFavorite } from "./favorites";
import { IntervalBox } from "./IntervalBox";
import { intervalLabel } from "./intervals";

export interface IntervalBarProps {
  /** The active interval, canonical. */
  value: string;
  /** Receives the canonical interval, from a favorite or from the box. */
  onChange: (interval: string) => void;
  /** Whether the box is open. The Chart screen drives it too (the digit typed on the chart opens it). */
  open: boolean;
  /** Text already in the box field when it opens. */
  initialText?: string;
  onOpenChange: (open: boolean) => void;
  secondsDisabledReason?: string | null;
  ticksDisabledReason?: string | null;
  returnFocusRef?: RefObject<HTMLElement | null>;
  /** Where the box sits: it is `position: absolute` against the nearest positioned ancestor. */
  boxStyle?: CSSProperties;
}

/**
 * The interval control of every chart: the favorites as buttons, and the "+" box where the student writes any
 * interval of the grammar (`2m`, `3h`, `2D`, `1W`, ranges). The Chart screen and the Terminal of a desk both use it,
 * so they always offer the same intervals. The favorites are shared and kept in this browser.
 */
export function IntervalBar({ value, onChange, open, initialText, onOpenChange, secondsDisabledReason, ticksDisabledReason, returnFocusRef, boxStyle }: IntervalBarProps) {
  const { t } = useI18n();
  const [favorites, setFavorites] = useState<string[]>(loadFavorites);
  const moreRef = useRef<HTMLButtonElement>(null);
  return (
    <>
      <Segmented
        label={t("chart.timeframe")}
        value={value}
        onChange={onChange}
        options={favorites.map((tf) => ({ value: tf, label: intervalLabel(tf, t) }))}
      />
      <button
        ref={moreRef}
        type="button"
        className="ib"
        data-testid="interval-more"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={t("chart.interval.more")}
        title={t("chart.interval.more")}
        onClick={() => onOpenChange(!open)}
      >
        +
      </button>
      {open ? (
        <IntervalBox
          value={value}
          favorites={favorites}
          initialText={initialText}
          onApply={onChange}
          onToggleFavorite={(interval) => setFavorites((list) => toggleFavorite(list, interval))}
          secondsDisabledReason={secondsDisabledReason}
          ticksDisabledReason={ticksDisabledReason}
          onClose={() => onOpenChange(false)}
          returnFocusRef={returnFocusRef}
          triggerRef={moreRef}
          style={boxStyle}
        />
      ) : null}
    </>
  );
}
