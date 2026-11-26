import { useEffect, useMemo, useState } from "react";
import { useApiQuery } from "@/api";
import type { QueryState } from "@/api";
import { Button, ErrorState, Input, Loading, Pane, Toggle } from "@/design";
import { useI18n } from "@/i18n";
import { errorText, hasCode, paths, verdictApi } from "./api";
import { formatDateTime, formatNumber } from "./format";
import { MEASURES, clampMeasure, inRange, numericValue, stepDecimals } from "./measures";
import type { MeasureMeta } from "./measures";
import type { LineId, MeasureValue, ThresholdSet, Verdict } from "./types";

type Values = Record<string, MeasureValue>;

/** True when the line has no thresholds at all (LP and RF start like that). */
export function lineHasNoThresholds(query: QueryState<ThresholdSet>): boolean {
  const error = query.error;
  return error !== undefined && (hasCode(error, "no_thresholds") || error.status === 404);
}

interface CopyProps {
  line: LineId;
  ownerBlocked: boolean;
  onDone: () => void;
}

/** The notice of a line with no thresholds and the button that creates them from SWC (7.3). */
export function CopyThresholdsNotice({ line, ownerBlocked, onDone }: CopyProps) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const copy = async () => {
    setBusy(true);
    setError(null);
    try {
      await verdictApi.copyThresholds(line, "SWC");
      onDone();
    } catch (cause) {
      setError(errorText(cause, t("verdict.error.generic")));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="bt-notice-box" role="status">
      <p className="p">{t("verdict.screen.noThresholds", { line })}</p>
      <Button variant="primary" onClick={() => void copy()} disabled={ownerBlocked || busy}>
        {t("verdict.screen.copyThresholds")}
      </Button>
      {error ? (
        <p className="err" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

function shown(meta: MeasureMeta, value: MeasureValue | undefined, locale: string, on: string, off: string): string {
  if (meta.kind === "toggle") return value === true ? on : off;
  return formatNumber(typeof value === "number" ? value : null, locale, stepDecimals(meta.step)) ?? "";
}

interface RowProps {
  meta: MeasureMeta;
  value: MeasureValue | undefined;
  disabled: boolean;
  onChange: (value: MeasureValue) => void;
}

function MeasureRow({ meta, value, disabled, onChange }: RowProps) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const label = t(`verdict.measure.${meta.id}.label`);
  const current = numericValue(value, meta.low);
  const [text, setText] = useState(String(current));
  useEffect(() => setText(String(current)), [current]);
  const invalid = text.trim() !== "" && !inRange(meta, Number(text));
  const helpId = `bt-help-${meta.id}`;

  const commit = (raw: string) => {
    const parsed = Number(raw.replace(",", "."));
    if (!Number.isFinite(parsed) || raw.trim() === "") {
      setText(String(current));
      return;
    }
    const next = clampMeasure(meta, parsed);
    setText(String(next));
    onChange(next);
  };

  return (
    <li className="bt-measure" data-measure={meta.id}>
      <div className="bt-measure-row">
        <span className="bt-measure-label">{label}</span>
        {meta.kind === "toggle" ? (
          <Toggle label={label} checked={value === true} disabled={disabled} onChange={(next) => onChange(next)} />
        ) : (
          <div className="bt-measure-controls">
            <input
              type="range"
              aria-label={t("verdict.th.slider", { label })}
              min={meta.low}
              max={meta.high}
              step={meta.step}
              value={current}
              disabled={disabled}
              onChange={(event) => onChange(clampMeasure(meta, Number(event.target.value)))}
            />
            <Input
              type="number"
              className="bt-measure-number"
              aria-label={t("verdict.th.value", { label })}
              aria-invalid={invalid ? true : undefined}
              min={meta.low}
              max={meta.high}
              step={meta.step}
              value={text}
              disabled={disabled}
              onChange={(event) => {
                setText(event.target.value);
                const parsed = Number(event.target.value);
                if (event.target.value.trim() !== "" && inRange(meta, parsed)) onChange(Number(parsed.toFixed(stepDecimals(meta.step))));
              }}
              onBlur={(event) => commit(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") commit((event.target as HTMLInputElement).value);
              }}
            />
          </div>
        )}
        <button
          type="button"
          className="ib bt-help"
          aria-expanded={open}
          aria-controls={helpId}
          aria-label={t("verdict.th.help", { label })}
          onClick={() => setOpen((v) => !v)}
        >
          ?
        </button>
      </div>
      {invalid ? (
        <p className="err" role="alert">
          {t("verdict.th.invalid", { low: String(meta.low), high: String(meta.high) })}
        </p>
      ) : null}
      {open ? (
        <dl id={helpId} className="bt-explain">
          {(["what", "why", "raise", "lower", "example"] as const).map((field) => (
            <div key={field}>
              <dt className="lbl">{t(`verdict.th.${field}`)}</dt>
              <dd className="p">{t(`verdict.measure.${meta.id}.${field}`)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </li>
  );
}

interface Props {
  line: LineId;
  current: QueryState<ThresholdSet>;
  /** The verdict on screen: tells whether it was made with the thresholds that are in force. */
  verdict: Verdict | null;
  ownerBlocked: boolean;
  /** The current thresholds changed (saved, restored or created): reload them. */
  onChanged: () => void;
  onRecomputed: (verdict: Verdict) => void;
  onClose: () => void;
}

function sameValues(a: Values, b: Values): boolean {
  return MEASURES.every((m) => a[m.key] === b[m.key]);
}

/** The editor of the threshold table of one line: 13 measures, explanations, draft until Save, history and restore. */
export function ThresholdsPanel({ line, current, verdict, ownerBlocked, onChanged, onRecomputed, onClose }: Props) {
  const { t, locale } = useI18n();
  /** The values being edited; null while nothing was touched (the saved values show). */
  const [edits, setEdits] = useState<Values | null>(null);
  const [reason, setReason] = useState("");
  const [showHistory, setShowHistory] = useState(false);
  const [busy, setBusy] = useState(false);
  const [recomputing, setRecomputing] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [savedVersion, setSavedVersion] = useState<number | null>(null);
  const history = useApiQuery<ThresholdSet[]>(showHistory ? paths.history(line) : null);

  const base = current.data;
  useEffect(() => setEdits(null), [base]);
  const draft: Values = edits ?? base?.values ?? {};

  const dirty = base !== undefined && !sameValues(draft, base.values);
  const pboOrder = numericValue(draft.pbo_warn, 0) > numericValue(draft.pbo_reject, 1);
  const latest = Math.max(base?.version ?? 0, savedVersion ?? 0);
  const stale = verdict !== null && verdict.line === line && base !== undefined && verdict.thresholds.version < latest;

  const act = async (work: () => Promise<ThresholdSet | null>, message: (set: ThresholdSet) => string) => {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const set = await work();
      if (set === null) {
        setNote(t("verdict.th.unchanged"));
      } else {
        setSavedVersion(set.version);
        setNote(message(set));
        setReason("");
        onChanged();
        if (showHistory) history.reload();
      }
    } catch (cause) {
      setError(errorText(cause, t("verdict.error.generic")));
    } finally {
      setBusy(false);
    }
  };

  const save = () =>
    act(
      async () => {
        const out = await verdictApi.saveThresholds(line, draft as Record<string, number | boolean>, reason.trim() || undefined);
        // A save that changes nothing is not written: the server answers the current set with `unchanged: true`.
        return out && typeof out.version === "number" && out.unchanged !== true ? out : null;
      },
      (set) => t("verdict.th.saved", { version: set.version }),
    );

  const recompute = async () => {
    if (!verdict) return;
    setBusy(true);
    setRecomputing(true);
    setError(null);
    try {
      onRecomputed(await verdictApi.recompute(verdict.verdict_id, "thresholds"));
      setNote(null);
    } catch (cause) {
      setError(errorText(cause, t("verdict.error.generic")));
    } finally {
      setBusy(false);
      setRecomputing(false);
    }
  };

  const ordered = useMemo(() => [...(history.data ?? [])].sort((a, b) => a.version - b.version), [history.data]);
  const changesOf = (set: ThresholdSet, index: number) => {
    const before = index > 0 ? ordered[index - 1] : undefined;
    if (!before) return [];
    return MEASURES.filter((m) => set.values[m.key] !== before.values[m.key]).map((m) => ({
      meta: m,
      before: shown(m, before.values[m.key], locale, t("verdict.th.on"), t("verdict.th.off")),
      after: shown(m, set.values[m.key], locale, t("verdict.th.on"), t("verdict.th.off")),
    }));
  };
  const who = (by: string) => (by === "owner" ? t("verdict.th.by.owner") : by === "system:seed" ? t("verdict.th.by.seed") : by);

  return (
    <Pane as="section" className="bt-block bt-thresholds" aria-label={t("verdict.th.title", { line, version: base?.version ?? "" })}>
      <header className="bt-block-head">
        <h2 className="h2">{t("verdict.th.title", { line, version: base?.version ?? "" })}</h2>
        <Button size="sm" variant="ghost" onClick={onClose}>
          {t("verdict.th.close")}
        </Button>
      </header>
      <div className="bt-block-body">
        {current.loading && !base ? <Loading inline /> : null}
        {current.error && !lineHasNoThresholds(current) ? <ErrorState message={current.error.message} onRetry={current.reload} /> : null}
        {lineHasNoThresholds(current) ? <CopyThresholdsNotice line={line} ownerBlocked={ownerBlocked} onDone={onChanged} /> : null}
        {base ? (
          <>
            {ownerBlocked ? (
              <p className="p bt-owner-note" role="status">
                {t("verdict.th.readOnly")} {t("verdict.owner.missing")}
              </p>
            ) : null}
            <ul className="bt-measures">
              {MEASURES.map((meta) => (
                <MeasureRow
                  key={meta.id}
                  meta={meta}
                  value={draft[meta.key]}
                  disabled={ownerBlocked || busy}
                  onChange={(value) => setEdits((prev) => ({ ...(prev ?? base.values), [meta.key]: value }))}
                />
              ))}
            </ul>
            {pboOrder ? (
              <p className="err" role="alert">
                {t("verdict.th.pboOrder")}
              </p>
            ) : null}
            <div className="bt-save-row">
              <label className="lbl" htmlFor="bt-th-reason">
                {t("verdict.th.reason")}
              </label>
              <Input id="bt-th-reason" value={reason} maxLength={500} disabled={ownerBlocked || busy} onChange={(event) => setReason(event.target.value)} />
              <Button variant="primary" onClick={() => void save()} disabled={ownerBlocked || busy || !dirty || pboOrder}>
                {t("verdict.th.save")}
              </Button>
              <Button variant="ghost" onClick={() => setEdits(null)} disabled={busy || !dirty}>
                {t("verdict.th.discard")}
              </Button>
            </div>
            {note ? (
              <p className="p bt-ok" role="status">
                {note}
              </p>
            ) : null}
            {error ? (
              <p className="err" role="alert">
                {error}
              </p>
            ) : null}
            {recomputing ? (
              <p className="p bt-ok" role="status">
                {t("verdict.recomputing")}
              </p>
            ) : null}
            {stale ? (
              <div className="bt-stale" role="status">
                <p className="p">{t("verdict.th.pending", { old: verdict?.thresholds.version ?? "", new: latest })}</p>
                <Button onClick={() => void recompute()} disabled={ownerBlocked || busy}>
                  {t("verdict.th.recompute")}
                </Button>
              </div>
            ) : null}
            <Button size="sm" variant="ghost" aria-expanded={showHistory} onClick={() => setShowHistory((v) => !v)}>
              {showHistory ? t("verdict.th.historyHide") : t("verdict.th.history")}
            </Button>
            {showHistory ? (
              <div className="bt-history">
                {history.loading && !history.data ? <Loading inline /> : null}
                {history.error ? <ErrorState message={history.error.message} onRetry={history.reload} /> : null}
                {history.data && ordered.length === 0 ? <p className="p">{t("verdict.th.historyEmpty")}</p> : null}
                <ol className="bt-history-list" reversed>
                  {[...ordered].reverse().map((set) => {
                    const index = ordered.indexOf(set);
                    const changes = changesOf(set, index);
                    const isCurrent = set.version === base.version;
                    return (
                      <li key={set.id} className="bt-history-item" data-version={set.version}>
                        <p className="p">
                          <b>{t("verdict.th.version", { version: set.version })}</b> | {formatDateTime(set.created_at, locale)} | {who(set.changed_by)}
                          {set.reason ? ` | ${set.reason}` : ""}
                          {isCurrent ? ` | ${t("verdict.th.current")}` : ""}
                        </p>
                        {index === 0 ? (
                          <p className="p">{t("verdict.th.firstVersion")}</p>
                        ) : (
                          <ul className="bt-changes">
                            {changes.map((c) => (
                              <li key={c.meta.id} className="p">
                                {t("verdict.th.change", { label: t(`verdict.measure.${c.meta.id}.label`), before: c.before, after: c.after })}
                              </li>
                            ))}
                          </ul>
                        )}
                        {isCurrent ? null : (
                          <Button
                            size="sm"
                            disabled={ownerBlocked || busy}
                            aria-label={`${t("verdict.th.restore")}: ${t("verdict.th.version", { version: set.version })}`}
                            onClick={() => void act(async () => {
                              const made = await verdictApi.restoreThresholds(line, set.id);
                              return made.unchanged === true ? null : made;
                            }, (made) => t("verdict.th.restored", { version: made.version }))}
                          >
                            {t("verdict.th.restore")}
                          </Button>
                        )}
                      </li>
                    );
                  })}
                </ol>
              </div>
            ) : null}
          </>
        ) : null}
      </div>
    </Pane>
  );
}
