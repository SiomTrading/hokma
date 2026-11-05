import { useApiQuery } from "@/api";
import { Button, EmptyState, ErrorState, Loading, Meter, Pane, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { CommandsCard, FirstDayCard, ShortcutsCard } from "@/screens/learn/cards";
import type { ScreenProps } from "@/shell/registry";
import { useShell } from "@/shell/useShell";

interface HomeData {
  ready: boolean;
  owner_name?: string;
  fund_name?: string;
  engine?: string;
  claude_status?: "connected" | "no_session" | "no_cli" | "unknown";
  agents?: { active: number; total: number };
  projects?: number;
  usage?: { used: number; cap: number | null; pct: number | null };
}

const CARD = { padding: 16, display: "flex", flexDirection: "column", gap: 8 } as const;

function period(hour: number): "morning" | "afternoon" | "evening" {
  if (hour < 12) return "morning";
  return hour < 19 ? "afternoon" : "evening";
}

/** Home: greeting by local time, the fund line, "Talk to my fund", four real numbers and the three Learn cards. */
export function HomeScreen(_props: ScreenProps) {
  const { t, locale } = useI18n();
  const shell = useShell();
  const home = useApiQuery<HomeData>("/api/home");

  if (home.loading && !home.data) return <Loading />;
  if (home.error || !home.data) return <ErrorState message={home.error?.message} onRetry={home.reload} />;
  const data = home.data;
  if (!data.ready) return <EmptyState icon="panel" title={t("home.notReady.title")} description={t("home.notReady.body")} />;

  const first = (data.owner_name ?? "").trim().split(/\s+/)[0] ?? "";
  const when = period(new Date().getHours());
  const greeting = first ? t(`home.greeting.${when}`, { name: first }) : t(`home.greeting.${when}Anon`);
  const usage = data.usage;
  const pct = usage?.pct ?? null;

  return (
    <section className="stagger" style={{ flex: 1, overflow: "auto", display: "flex", flexDirection: "column", gap: 8 }}>
      <Pane as="section" style={{ flex: "none", padding: 22, display: "flex", justifyContent: "space-between", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <h1 className="disp">{greeting}</h1>
          {data.fund_name ? <p className="p">{t("home.fund", { fund: data.fund_name })}</p> : null}
        </div>
        <Button variant="primary" onClick={() => shell.openScreen("chat")}>
          {t("home.talk")}
        </Button>
      </Pane>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: 8 }}>
        <Pane style={CARD} as="section" aria-label={t("home.engine")}>
          <span className="lbl">{t("home.engine")}</span>
          <span className="h2">{t(`home.engine.${data.engine ?? "claude_code"}`)}</span>
          <Pill tone={data.claude_status === "connected" ? "ok" : "warn"} style={{ alignSelf: "flex-start" }}>
            <span className={data.claude_status === "connected" ? "dot live" : "dot"} aria-hidden="true" />
            {t(`home.claude.${data.claude_status ?? "unknown"}`)}
          </Pill>
        </Pane>
        <Pane style={CARD} as="section" aria-label={t("home.agents")}>
          <span className="lbl">{t("home.agents")}</span>
          <span className="mono" style={{ fontSize: 26 }}>
            {data.agents?.active ?? 0} / {data.agents?.total ?? 0}
          </span>
        </Pane>
        <Pane style={CARD} as="section" aria-label={t("home.projects")}>
          <span className="lbl">{t("home.projects")}</span>
          <span className="mono" style={{ fontSize: 26 }}>{data.projects ?? 0}</span>
        </Pane>
        <Pane style={CARD} as="section" aria-label={t("home.cap")}>
          <span className="lbl">{t("home.cap")}</span>
          {pct !== null && usage?.cap ? (
            <>
              <span className="mono" style={{ fontSize: 26 }}>{t("home.cap.used", { pct: Math.round(pct) })}</span>
              <Meter value={pct} tone={pct >= 90 ? "bad" : pct >= 70 ? "warn" : "acc"} label={t("home.cap")} />
              <span className="p" style={{ fontSize: 12 }}>
                {t("home.cap.tokens", { used: usage.used.toLocaleString(locale), cap: usage.cap.toLocaleString(locale) })}
              </span>
            </>
          ) : (
            <>
              <span className="p" style={{ fontSize: 12 }}>{t("home.cap.unset")}</span>
              <div>
                <Button size="sm" onClick={() => shell.openSettings("models")}>{t("home.cap.set")}</Button>
              </div>
            </>
          )}
        </Pane>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gap: 8 }} aria-label={t("home.cards")}>
        <FirstDayCard onOpenGuide={() => shell.openScreen("learn")} />
        <CommandsCard />
        <ShortcutsCard />
      </div>
    </section>
  );
}
