import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/api";
import { Button, cx, ErrorState, Icon, Isotype, Loading, Pill } from "@/design";
import type { IconName } from "@/design";
import { useI18n } from "@/i18n";
import { setChatStart } from "@/screens/chat/start";
import { newDraftId } from "@/shell/tabsStorage";
import { useShell } from "@/shell/useShell";
import { formatPercent } from "./format";
import { todayApi } from "./todayApi";
import type { TodayAsset, TodayData, TodayEvent } from "./todayApi";

type Shell = ReturnType<typeof useShell>;
type Translate = ReturnType<typeof useI18n>["t"];

const POLL_MS = 5000;
const POLL_TRIES = 36;
const IMPACT = { low: 1, medium: 2, high: 3 } as const;

/** "Adjust with Hokma": a new chat whose first message asks to change the card. The owner reads it and sends it. */
export function askHokmaForToday(shell: Shell, t: Translate): void {
  const draft = newDraftId();
  setChatStart(draft, t("desks.today.hokma.start"));
  shell.openScreen("chat", { draft });
  shell.toast({ tone: "info", text: t("desks.today.hokma.toast") });
}

const isToday = (iso: string) => new Date(iso).toDateString() === new Date().toDateString();
const clock = (iso: string | null, locale: string) =>
  iso ? new Date(iso).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" }) : "";

function Cell({ icon, title, meta, children }: { icon: IconName; title: string; meta?: string; children: React.ReactNode }) {
  return (
    <div className="today-cell">
      <div className="today-cell-hd">
        <h3><span className="today-cell-ic"><Icon name={icon} size={14} /></span>{title}</h3>
        {meta ? <span className="today-meta">{meta}</span> : null}
      </div>
      {children}
    </div>
  );
}

/** A block that has nothing to show says why and offers the next step. */
function BlockState({ title, text, children }: { title: string; text?: string; children?: React.ReactNode }) {
  return (
    <div className="today-state">
      <h4>{title}</h4>
      {text ? <p>{text}</p> : null}
      {children ? <div className="today-state-acts">{children}</div> : null}
    </div>
  );
}

function Watchlist({ data, onAsk, onConnect }: { data: TodayData["watchlist"]; onAsk: () => void; onConnect: () => void }) {
  const { t, locale } = useI18n();
  if (!data) return null;
  const rows = data.assets ?? [];
  const title = t("desks.today.watch.title");
  let body;
  if (data.error) {
    body = (
      <BlockState title={t("desks.today.noConnection")} text={data.error}>
        <Button variant="primary" size="sm" onClick={onConnect}>{t("desks.today.goConnections")}</Button>
      </BlockState>
    );
  } else if (rows.length === 0) {
    body = (
      <BlockState title={t("desks.today.watch.empty.title")} text={t("desks.today.watch.empty.text")}>
        <Button size="sm" onClick={onAsk}>{t("desks.today.adjust")}</Button>
      </BlockState>
    );
  } else {
    body = (
      <table className="today-tbl">
        <thead>
          <tr>
            <th scope="col">{t("desks.today.watch.asset")}</th>
            <th scope="col">{t("desks.today.watch.desks")}</th>
            <th scope="col" className="r">{t("desks.today.watch.price")}</th>
            <th scope="col" className="r">{t("desks.today.watch.change")}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((a: TodayAsset) => (
            <tr key={a.symbol}>
              <th scope="row" className="mono">{a.symbol}</th>
              <td>{a.desks.length ? a.desks.map((d) => <span key={d} className="today-chip">{d}</span>) : <span className="today-chip extra">{t("desks.today.watch.extra")}</span>}</td>
              <td className="r mono" title={a.error}>{a.last === null ? t("desks.today.watch.noPrice") : a.last.toLocaleString(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
              <td className="r">
                {a.change_pct == null ? <span className="today-chg">{t("desks.stat.none")}</span> : (
                  <span className={cx("today-chg", a.change_pct > 0 ? "up" : a.change_pct < 0 ? "down" : "")}>{(a.change_pct > 0 ? "+" : "") + formatPercent(a.change_pct, locale)}</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  }
  return <Cell icon="chart" title={title} meta={rows.length ? t("desks.today.watch.count", { n: String(rows.length) }) : undefined}>{body}</Cell>;
}

function Calendar({ data, onAsk }: { data: TodayData["calendar"]; onAsk: () => void }) {
  const { t, locale } = useI18n();
  if (!data) return null;
  const events = data.events ?? [];
  let body;
  if (data.error) body = <BlockState title={t("desks.today.cal.error")} text={data.error} />;
  else if (events.length === 0) {
    body = (
      <BlockState title={t("desks.today.cal.empty.title")} text={t("desks.today.cal.empty.text")}>
        <Button size="sm" onClick={onAsk}>{t("desks.today.adjust")}</Button>
      </BlockState>
    );
  } else {
    body = (
      <div className="today-scroll">
        <table className="today-tbl">
          <thead>
            <tr>
              <th scope="col">{t("desks.today.cal.time")}</th>
              <th scope="col">{t("desks.today.cal.impact")}</th>
              <th scope="col">{t("desks.today.cal.event")}</th>
              <th scope="col" className="r">{t("desks.today.cal.actual")}</th>
              <th scope="col" className="r">{t("desks.today.cal.forecast")}</th>
              <th scope="col" className="r">{t("desks.today.cal.previous")}</th>
            </tr>
          </thead>
          <tbody>
            {events.map((e: TodayEvent, i) => (
              <tr key={`${e.time}-${i}`} className={cx(new Date(e.time).getTime() < Date.now() && "past", e.impact === "high" && "high")}>
                <td className="mono">{clock(e.time, locale)} <span className="today-cur">{e.country}</span></td>
                <td>
                  <span className={cx("today-imp", `l${IMPACT[e.impact]}`)} role="img" aria-label={t(`desks.today.cal.impact.${e.impact}`)} title={t(`desks.today.cal.impact.${e.impact}`)}><i /><i /><i /></span>
                </td>
                <td className="today-ev">{e.event}</td>
                <td className="r mono">{e.actual ?? "·"}</td>
                <td className="r mono">{e.estimate ?? "·"}</td>
                <td className="r mono">{e.previous ?? "·"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  return <Cell icon="tasks" title={t("desks.today.cal.title")} meta={data.source}>{body}</Cell>;
}

function News({ data, onAsk, onConnect }: { data: TodayData["news"]; onAsk: () => void; onConnect: () => void }) {
  const { t, locale } = useI18n();
  if (!data) return null;
  const items = data.news ?? [];
  let body;
  if (data.error) {
    body = (
      <BlockState title={t("desks.today.noConnection")} text={data.error}>
        <Button variant="primary" size="sm" onClick={onConnect}>{t("desks.today.goConnections")}</Button>
      </BlockState>
    );
  } else if (items.length === 0) {
    body = (
      <BlockState title={t("desks.today.news.empty.title")} text={t("desks.today.news.empty.text")}>
        <Button size="sm" onClick={onAsk}>{t("desks.today.adjust")}</Button>
      </BlockState>
    );
  } else {
    body = (
      <ul className="today-news">
        {items.map((n, i) => (
          <li key={`${n.time}-${i}`}>
            <time className="mono">{clock(n.time, locale)}</time>
            <div><h4>{n.headline}</h4><span>{n.source}</span></div>
          </li>
        ))}
      </ul>
    );
  }
  return <Cell icon="news" title={t("desks.today.news.title")}>{body}</Cell>;
}

interface BriefProps {
  data: TodayData["briefing"];
  generating: boolean;
  error: string | null;
  onGenerate: () => void;
  onTasks: () => void;
}

function Briefing({ data, generating, error, onGenerate, onTasks }: BriefProps) {
  const { t, locale } = useI18n();
  const fresh = data !== null && isToday(data.as_of);
  const generate = (
    <Button size="sm" variant={fresh ? "default" : "primary"} disabled={generating} onClick={onGenerate}>
      {generating ? t("desks.today.brief.generating") : t("desks.today.brief.generate")}
    </Button>
  );
  const pill = <Pill tone={fresh ? "ok" : "warn"}>{t(fresh ? "desks.today.brief.today" : "desks.today.brief.pending")}</Pill>;
  return (
    <Cell icon="panel" title={t("desks.today.brief.title")}>
      <div className="today-brief">
        {pill}
        {fresh ? (
          <p className="today-brief-text">{data.text}</p>
        ) : (
          <BlockState title={t("desks.today.brief.none.title")} text={t("desks.today.brief.none.text")} />
        )}
        {!fresh && data ? <p className="today-brief-prev"><b>{t("desks.today.brief.last", { date: new Date(data.as_of).toLocaleDateString(locale, { day: "numeric", month: "short" }) })}</b> {data.text.slice(0, 200)}</p> : null}
        {error ? <span className="err" role="alert">{error}</span> : null}
        <div className="today-state-acts">
          {generate}
          <Button variant="ghost" size="sm" onClick={onTasks}>{t("desks.today.brief.tasks")}</Button>
        </div>
      </div>
    </Cell>
  );
}

/** The Today card of the Fund: watchlist, economic calendar, news and the premarket briefing, common to all desks. */
export function TodayCard() {
  const { t } = useI18n();
  const shell = useShell();
  const [data, setData] = useState<TodayData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [briefError, setBriefError] = useState<string | null>(null);
  const alive = useRef(true);
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);
  const failure = (cause: unknown) => (cause instanceof ApiError ? cause.message : t("desks.today.error"));

  const load = useCallback(async (fresh = false) => {
    setBusy(true);
    try {
      const next = await (fresh ? todayApi.refresh() : todayApi.get());
      if (!alive.current) return null;
      setData(next);
      setError(null);
      return next;
    } catch (cause) {
      if (alive.current) setError(cause instanceof ApiError ? cause.message : t("desks.today.error"));
      return null;
    } finally {
      if (alive.current) setBusy(false);
    }
  }, [t]);

  useEffect(() => {
    alive.current = true;
    void load();
    return () => {
      alive.current = false;
      if (poll.current) clearInterval(poll.current);
    };
  }, [load]);

  /** After "Generate now" the routine runs in the background: look again until a newer briefing shows up. */
  const waitForBriefing = (before: number | null) => {
    let tries = 0;
    if (poll.current) clearInterval(poll.current);
    poll.current = setInterval(() => {
      tries += 1;
      void load().then((next) => {
        const done = next?.briefing && next.briefing.task_id !== before;
        if (done || tries >= POLL_TRIES) {
          if (poll.current) clearInterval(poll.current);
          setGenerating(false);
        }
      });
    }, POLL_MS);
  };

  const generate = async () => {
    setGenerating(true);
    setBriefError(null);
    try {
      const result = await todayApi.generateBriefing(data);
      if (result === "needs-approval") {
        setGenerating(false);
        shell.toast({ tone: "info", text: t("desks.today.brief.needsApproval") });
        shell.openScreen("tasks");
        return;
      }
      shell.toast({ tone: "info", text: t("desks.today.brief.started") });
      waitForBriefing(data?.briefing?.task_id ?? null);
    } catch (cause) {
      setGenerating(false);
      setBriefError(failure(cause));
    }
  };

  const ask = () => askHokmaForToday(shell, t);
  const connect = () => shell.openScreen("connections");
  return (
    <section className="pane today" aria-label={t("desks.today.title")}>
      <div className="today-hd">
        <h2 className="disp">{t("desks.today.title")}</h2>
        <span className="today-sub">{t("desks.today.sub")}</span>
        <span className="desk-hdr-actions">
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => void load(true)} aria-label={t("desks.today.refresh")}>
            {t("desks.today.refresh")}
          </Button>
          <Button size="sm" onClick={ask}><Isotype size={14} />{t("desks.today.adjust")}</Button>
        </span>
      </div>
      {data === null && error ? (
        <ErrorState message={error} onRetry={() => void load()} />
      ) : data === null ? (
        <Loading label={t("desks.today.loading")} />
      ) : (
        <div className="today-grid">
          <Watchlist data={data.watchlist} onAsk={ask} onConnect={connect} />
          <Calendar data={data.calendar} onAsk={ask} />
          <News data={data.news} onAsk={ask} onConnect={connect} />
          {data.config.sections.briefing ? (
            <Briefing data={data.briefing} generating={generating} error={briefError} onGenerate={() => void generate()} onTasks={() => shell.openScreen("tasks")} />
          ) : null}
        </div>
      )}
    </section>
  );
}
