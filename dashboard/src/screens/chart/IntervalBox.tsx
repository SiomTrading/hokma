import { useId, useMemo, useState } from "react";
import type { CSSProperties, KeyboardEvent, RefObject } from "react";
import { Button, Input, Segmented, cx } from "@/design";
import { useI18n } from "@/i18n";
import { ChartPopover, returnFocus } from "./ChartPopover";
import { MAX_FAVORITES, isFavorite } from "./favorites";
import { intervalErrorText, intervalLabel, intervalSuggestions, isSeconds, parseInterval } from "./intervals";
import type { RangeUnit } from "./intervals";

export interface IntervalBoxProps {
  /** The active interval, canonical. */
  value: string;
  favorites: readonly string[];
  /** Receives the canonical interval. The box closes right after and gives the focus back. */
  onApply: (interval: string) => void;
  /** Receives the canonical interval to pin or unpin. */
  onToggleFavorite: (interval: string) => void;
  onClose: () => void;
  /** When set, seconds cannot be drawn for this symbol: their suggestions show disabled with this text. */
  secondsDisabledReason?: string | null;
  /** When set, the asset has no known tick size: range is offered in price only, with this text. */
  ticksDisabledReason?: string | null;
  /** Text already in the field when it opens (the digit typed on the chart). */
  initialText?: string;
  returnFocusRef?: RefObject<HTMLElement | null>;
  triggerRef?: RefObject<HTMLElement | null>;
  style?: CSSProperties;
  className?: string;
}

interface Item {
  canonical: string;
  label: string;
  hint: string;
  disabled: boolean;
}

function StarIcon({ filled }: { filled: boolean }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true" fill={filled ? "currentColor" : "none"} stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round">
      <path d="M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z" />
    </svg>
  );
}

/**
 * The interval box: write any interval of the grammar (`30s`, `7m`, `3h`, `2D`, `1W`, `10r`, `2.5rp`), pick a
 * suggestion, build a range in ticks or price, and pin the interval to the favorites. Enter applies.
 */
export function IntervalBox({
  value,
  favorites,
  onApply,
  onToggleFavorite,
  onClose,
  secondsDisabledReason,
  ticksDisabledReason,
  initialText,
  returnFocusRef,
  triggerRef,
  style,
  className,
}: IntervalBoxProps) {
  const { t } = useI18n();
  const ids = useId();
  const listId = `${ids}-list`;
  const errorId = `${ids}-error`;
  const rangeErrorId = `${ids}-range-error`;
  const [text, setText] = useState(initialText ?? "");
  const [active, setActive] = useState(-1);
  const [attempt, setAttempt] = useState<string | null>(null);
  const current = parseInterval(value);
  const startUnit: RangeUnit = ticksDisabledReason ? "p" : current.ok && current.interval.kind === "range" ? current.interval.unit : "t";
  const [rangeUnit, setRangeUnit] = useState<RangeUnit>(startUnit);
  const [rangeSize, setRangeSize] = useState(current.ok && current.interval.kind === "range" ? String(current.interval.size) : "");
  const [rangeError, setRangeError] = useState<string | null>(null);

  const parsed = parseInterval(text);
  const typed = text.trim();
  const secondsBlocked = (canonical: string) => !!secondsDisabledReason && isSeconds(canonical);

  const items = useMemo<Item[]>(() => {
    const source = typed
      ? intervalSuggestions(typed).map((s) => ({ canonical: s.canonical, hint: t(`chart.interval.unit.${s.unit}`) }))
      : favorites.map((canonical) => ({ canonical, hint: "" }));
    return source.map(({ canonical, hint }) => ({
      canonical,
      label: intervalLabel(canonical, t),
      hint,
      disabled: !!secondsDisabledReason && isSeconds(canonical),
    }));
  }, [typed, favorites, secondsDisabledReason, t]);

  const apply = (canonical: string) => {
    onApply(canonical);
    onClose();
    returnFocus(returnFocusRef);
  };

  /** What Enter would apply, or the error to show. */
  const check = (candidate: string): string | null => {
    const result = parseInterval(candidate);
    if (!result.ok) return intervalErrorText(result.error, t);
    if (secondsBlocked(result.interval.canonical)) return secondsDisabledReason ?? null;
    if (result.interval.kind === "range" && result.interval.unit === "t" && ticksDisabledReason) return ticksDisabledReason;
    return null;
  };

  const liveError = parsed.ok ? null : parsed.error === "months" ? intervalErrorText("months", t) : null;
  const error = attempt !== null ? check(attempt) : liveError;

  const onFieldKey = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      const enabled = items.map((item, index) => (item.disabled ? -1 : index)).filter((index) => index >= 0);
      if (enabled.length === 0) return;
      event.preventDefault();
      const at = enabled.indexOf(active);
      const next = event.key === "ArrowDown" ? enabled[(at + 1) % enabled.length]! : enabled[(at - 1 + enabled.length) % enabled.length]!;
      setActive(next);
      return;
    }
    if (event.key !== "Enter") return;
    event.preventDefault();
    const chosen = active >= 0 ? items[active] : undefined;
    if (chosen && !chosen.disabled) {
      apply(chosen.canonical);
      return;
    }
    if (check(text) === null && parsed.ok) {
      apply(parsed.interval.canonical);
      return;
    }
    setAttempt(text);
  };

  const applyRange = () => {
    const candidate = `r:${rangeSize.trim()}${rangeUnit}`;
    const result = parseInterval(candidate);
    if (!result.ok) {
      setRangeError(t("chart.interval.rangeInvalid"));
      return;
    }
    if (result.interval.unit === "t" && ticksDisabledReason) {
      setRangeError(ticksDisabledReason);
      return;
    }
    apply(result.interval.canonical);
  };

  const starTarget = parsed.ok ? parsed.interval.canonical : value;
  const starred = isFavorite(favorites, starTarget);
  const full = !starred && favorites.length >= MAX_FAVORITES;
  const starLabel = starred ? t("chart.interval.removeFavorite") : t("chart.interval.addFavorite");
  const activeId = active >= 0 && items[active] ? `${ids}-opt-${active}` : undefined;

  return (
    <ChartPopover label={t("chart.interval.label")} onClose={onClose} returnFocusRef={returnFocusRef} triggerRef={triggerRef} style={style} className={className}>
      <label htmlFor={`${ids}-field`} className="chart-pop-head">
        {t("chart.interval.label")}
      </label>
      <div className="chart-pop-row">
        <Input
          id={`${ids}-field`}
          role="combobox"
          aria-expanded={items.length > 0}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={activeId}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errorId : undefined}
          autoFocus
          value={text}
          placeholder={t("chart.interval.placeholder")}
          autoComplete="off"
          spellCheck={false}
          maxLength={16}
          onChange={(event) => {
            setText(event.target.value);
            setActive(-1);
            setAttempt(null);
          }}
          onFocus={(event) => {
            const end = event.target.value.length;
            event.target.setSelectionRange(end, end);
          }}
          onKeyDown={onFieldKey}
          style={{ minHeight: 34, fontSize: 13 }}
        />
        <button
          type="button"
          className="ib chart-star"
          aria-label={starLabel}
          aria-pressed={starred}
          title={full ? t("chart.interval.favoritesFull") : starLabel}
          disabled={full}
          onClick={() => onToggleFavorite(starTarget)}
        >
          <StarIcon filled={starred} />
        </button>
      </div>
      {error ? (
        <p id={errorId} className="chart-pop-error" role="alert">
          {error}
        </p>
      ) : null}

      {items.length > 0 ? (
        <>
          <span className="chart-pop-head" id={`${ids}-list-head`}>
            {typed ? t("chart.interval.suggestions") : t("chart.interval.favorites")}
          </span>
          <div className="chart-pop-list" role="listbox" id={listId} aria-labelledby={`${ids}-list-head`}>
            {items.map((item, index) => (
              <div
                key={item.canonical}
                id={`${ids}-opt-${index}`}
                role="option"
                className={cx("chart-opt", index === active && "active", item.canonical === value && "current")}
                aria-selected={index === active}
                aria-disabled={item.disabled || undefined}
                title={item.disabled ? (secondsDisabledReason ?? undefined) : undefined}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  if (!item.disabled) apply(item.canonical);
                }}
              >
                <span className="mono">{item.label}</span>
                {item.hint ? <span className="h">{item.hint}</span> : null}
              </div>
            ))}
          </div>
        </>
      ) : null}

      <span className="chart-pop-head" id={`${ids}-range-head`}>
        {t("chart.interval.range")}
      </span>
      <div className="chart-pop-row" role="group" aria-labelledby={`${ids}-range-head`}>
        <Input
          aria-label={t("chart.interval.rangeSize")}
          aria-invalid={rangeError ? true : undefined}
          aria-describedby={rangeError ? rangeErrorId : undefined}
          inputMode="decimal"
          value={rangeSize}
          placeholder={rangeUnit === "t" ? "10" : "2.5"}
          autoComplete="off"
          spellCheck={false}
          maxLength={12}
          onChange={(event) => {
            setRangeSize(event.target.value);
            setRangeError(null);
          }}
          onKeyDown={(event) => {
            if (event.key !== "Enter") return;
            event.preventDefault();
            applyRange();
          }}
          style={{ minHeight: 34, fontSize: 13, flex: 1 }}
        />
        {ticksDisabledReason ? (
          <span className="p" style={{ fontSize: 12 }}>
            {t("chart.interval.price")}
          </span>
        ) : (
          <Segmented<RangeUnit>
            label={t("chart.interval.rangeUnit")}
            value={rangeUnit}
            onChange={(unit) => {
              setRangeUnit(unit);
              setRangeError(null);
            }}
            options={[
              { value: "t", label: t("chart.interval.ticks") },
              { value: "p", label: t("chart.interval.price") },
            ]}
          />
        )}
        <Button size="sm" onClick={applyRange}>
          {t("chart.interval.apply")}
        </Button>
      </div>
      {rangeError ? (
        <p id={rangeErrorId} className="chart-pop-error" role="alert">
          {rangeError}
        </p>
      ) : ticksDisabledReason ? (
        <p className="p" style={{ fontSize: 11, padding: "0 6px" }}>
          {ticksDisabledReason}
        </p>
      ) : null}
    </ChartPopover>
  );
}
