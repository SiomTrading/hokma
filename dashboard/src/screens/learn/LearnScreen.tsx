import { useEffect, useState } from "react";
import { useApiQuery } from "@/api";
import type { QueryState } from "@/api";
import { Button, EmptyState, ErrorState, Icon, Input, Loading, Pane } from "@/design";
import { useI18n } from "@/i18n";
import type { ScreenProps } from "@/shell/registry";
import { CommandsCard, FirstDayCard, ShortcutsCard } from "./cards";
import { Markdown } from "./Markdown";
import type { SearchHit, Topic, TopicSummary } from "./types";

const FIRST_STEPS = "first-steps";
const MIN_QUERY = 2;

function useDebounced(value: string, ms: number): string {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return debounced;
}

/** Learn: search the guide, the three cards of the design, the list of topics and the reader. */
export function LearnScreen(_props: ScreenProps) {
  const { t, locale } = useI18n();
  const [query, setQuery] = useState("");
  const [slug, setSlug] = useState<string | null>(null);
  const term = useDebounced(query.trim(), 200);
  const searching = term.length >= MIN_QUERY;

  const topics = useApiQuery<TopicSummary[]>(`/api/learn/topics?lang=${locale}`);
  const hits = useApiQuery<SearchHit[]>(searching ? `/api/learn/search?q=${encodeURIComponent(term)}&lang=${locale}` : null);

  const open = (next: string) => {
    setSlug(next);
    setQuery("");
  };

  if (slug) return <Reader slug={slug} locale={locale} onBack={() => setSlug(null)} />;

  return (
    <section className="stagger" style={{ flex: 1, display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))", gridAutoRows: "min-content", gap: 8, overflow: "auto" }}>
      <Pane style={{ gridColumn: "1 / -1", padding: 12 }}>
        <label htmlFor="learn-search" style={{ position: "absolute", left: -9999 }}>
          {t("learn.search.label")}
        </label>
        <Input id="learn-search" type="search" role="searchbox" placeholder={t("learn.search.placeholder")} value={query} onChange={(event) => setQuery(event.target.value)} autoComplete="off" />
      </Pane>

      {searching ? (
        <SearchResults state={hits} pendingTerm={query.trim() !== term} onOpen={open} />
      ) : (
        <>
          <FirstDayCard onOpenGuide={() => open(FIRST_STEPS)} />
          <CommandsCard />
          <ShortcutsCard />
          <TopicList state={topics} onOpen={open} />
        </>
      )}
    </section>
  );
}

function SearchResults({ state, pendingTerm, onOpen }: { state: QueryState<SearchHit[]>; pendingTerm: boolean; onOpen: (slug: string) => void }) {
  const { t } = useI18n();
  if ((state.loading && !state.data) || pendingTerm) return <Pane style={{ gridColumn: "1 / -1" }}><Loading /></Pane>;
  if (state.error) return <Pane style={{ gridColumn: "1 / -1" }}><ErrorState message={state.error.message} onRetry={state.reload} /></Pane>;
  const found = state.data ?? [];
  if (found.length === 0) {
    return (
      <Pane style={{ gridColumn: "1 / -1" }}>
        <EmptyState icon="search" title={t("learn.search.none")} description={t("learn.search.noneBody")} />
      </Pane>
    );
  }
  return (
    <Pane style={{ gridColumn: "1 / -1", padding: "4px 0" }} as="section" aria-label={t("learn.search.results")}>
      {found.map((hit) => (
        <button key={hit.slug} type="button" className="row" onClick={() => onOpen(hit.slug)} style={{ width: "100%", textAlign: "left", background: "none", border: 0, borderTop: "1px solid var(--border)", color: "inherit", cursor: "pointer", flexDirection: "column", alignItems: "flex-start", gap: 4 }}>
          <span className="h2">{hit.title}</span>
          <span className="p" style={{ fontSize: 12 }}>{hit.snippet}</span>
        </button>
      ))}
    </Pane>
  );
}

function TopicList({ state, onOpen }: { state: QueryState<TopicSummary[]>; onOpen: (slug: string) => void }) {
  const { t } = useI18n();
  return (
    <Pane style={{ padding: 18, display: "flex", flexDirection: "column", gap: 8 }} as="section" aria-label={t("learn.topics")}>
      <span className="lbl">{t("learn.topics")}</span>
      {state.loading && !state.data ? <Loading inline /> : null}
      {state.error ? <ErrorState message={state.error.message} onRetry={state.reload} /> : null}
      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        {(state.data ?? []).map((topic) => (
          <button key={topic.slug} type="button" className="nav" onClick={() => onOpen(topic.slug)} style={{ justifyContent: "space-between" }}>
            <span>{topic.title}</span>
            <Icon name="chevronRight" size={14} />
          </button>
        ))}
      </div>
    </Pane>
  );
}

function Reader({ slug, locale, onBack }: { slug: string; locale: string; onBack: () => void }) {
  const { t } = useI18n();
  const topic = useApiQuery<Topic>(`/api/learn/topics/${encodeURIComponent(slug)}?lang=${locale}`);
  return (
    <section className="fade" style={{ flex: 1, overflow: "auto", display: "flex", flexDirection: "column", gap: 8 }}>
      <div>
        <Button size="sm" variant="ghost" onClick={onBack}>
          {t("learn.back")}
        </Button>
      </div>
      <Pane style={{ padding: "20px 24px" }} as="section">
        {topic.loading && !topic.data ? <Loading /> : null}
        {topic.error ? <ErrorState message={topic.error.message} onRetry={topic.reload} /> : null}
        {topic.data ? <Markdown source={topic.data.body} /> : null}
      </Pane>
    </section>
  );
}
