import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Button, Icon, Input, Pill } from "@/design";
import { useI18n } from "@/i18n";
import * as client from "./documentsApi";
import { MAX_LISTED_LINKS, MAX_MARKED_LINKS } from "./documentsTypes";
import type { DiscoveredItem, DocsScope, FetchReason } from "./documentsTypes";

type RowState =
  | { phase: "idle" }
  | { phase: "waiting" }
  | { phase: "running" }
  | { phase: "done" }
  | { phase: "failed"; reason: FetchReason | null };

interface Row {
  item: DiscoveredItem;
  marked: boolean;
  state: RowState;
}

const isWebAddress = (value: string): boolean => /^https?:\/\/\S+$/i.test(value);

/** Paste a link, find the documents it offers, mark the ones wanted and download them one by one. */
export function DocumentLinks({ projectId, folderId = null, onAdded }: { projectId: DocsScope; folderId?: number | null; onAdded: () => void | Promise<void> }) {
  const { t } = useI18n();
  const [address, setAddress] = useState("");
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [rows, setRows] = useState<Row[] | null>(null);
  const [downloading, setDownloading] = useState(false);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // Another project: start from a clean field.
  useEffect(() => {
    setAddress("");
    setRows(null);
    setSearchError(null);
  }, [projectId]);

  const search = async (event?: FormEvent) => {
    event?.preventDefault();
    const url = address.trim();
    if (!isWebAddress(url)) {
      setSearchError(t("docs.link.error.invalid"));
      return;
    }
    setSearching(true);
    setSearchError(null);
    setRows(null);
    try {
      const found = await client.discoverDocuments(projectId, url);
      if (!alive.current) return;
      const items = found.items.slice(0, MAX_LISTED_LINKS);
      setRows(items.map((item) => ({ item, marked: found.page_is_document && items.length === 1, state: { phase: "idle" } })));
    } catch {
      if (alive.current) setSearchError(t("docs.link.error.search"));
    } finally {
      if (alive.current) setSearching(false);
    }
  };

  const marked = rows?.filter((r) => r.marked && r.state.phase !== "done").length ?? 0;

  const toggle = (index: number) =>
    setRows((prev) =>
      prev
        ? prev.map((row, i) => {
            if (i !== index) return row;
            if (!row.marked && prev.filter((r) => r.marked).length >= MAX_MARKED_LINKS) return row;
            return { ...row, marked: !row.marked };
          })
        : prev,
    );

  const markAll = () =>
    setRows((prev) => {
      if (!prev) return prev;
      let left = MAX_MARKED_LINKS;
      return prev.map((row) => {
        if (row.state.phase === "done") return row;
        const take = left > 0;
        if (take) left -= 1;
        return { ...row, marked: take };
      });
    });

  const clearMarks = () => setRows((prev) => (prev ? prev.map((row) => ({ ...row, marked: false })) : prev));

  const setState = useCallback((index: number, state: RowState) => {
    setRows((prev) => (prev ? prev.map((row, i) => (i === index ? { ...row, state } : row)) : prev));
  }, []);

  /** One at a time: the backend downloads and indexes inside each request and pauses between downloads of one site. */
  const download = async () => {
    if (!rows || downloading) return;
    const queue = rows.map((row, index) => ({ row, index })).filter(({ row }) => row.marked && row.state.phase !== "done");
    if (queue.length === 0) return;
    setDownloading(true);
    queue.forEach(({ index }) => setState(index, { phase: "waiting" }));
    for (const { row, index } of queue) {
      if (!alive.current) return;
      setState(index, { phase: "running" });
      try {
        await client.fetchDocument(projectId, row.item, folderId);
        if (!alive.current) return;
        setRows((prev) => (prev ? prev.map((r, i) => (i === index ? { ...r, marked: false, state: { phase: "done" } } : r)) : prev));
        await onAdded();
      } catch (cause) {
        if (!alive.current) return;
        setState(index, { phase: "failed", reason: cause instanceof client.FetchFailure ? cause.reason : null });
      }
    }
    if (alive.current) setDownloading(false);
  };

  const reasonText = (reason: FetchReason | null): string =>
    reason === "duplicate" && projectId === "library" ? t("library.error.duplicate") : t(`docs.link.reason.${reason ?? "generic"}`);

  return (
    <section aria-label={t("docs.link.title")} data-testid="documents-links" style={{ margin: "0 10px 10px", display: "flex", flexDirection: "column", gap: 6 }}>
      <form onSubmit={(event) => void search(event)} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <span className="lbl">{t("docs.link.title")}</span>
        <div style={{ display: "flex", gap: 8 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <Input
              aria-label={t("docs.link.title")}
              placeholder="https://"
              inputMode="url"
              autoComplete="off"
              spellCheck={false}
              value={address}
              onChange={(event) => {
                setAddress(event.target.value);
                setSearchError(null);
              }}
            />
          </div>
          <Button size="sm" variant="primary" type="submit" disabled={searching || downloading || address.trim() === ""}>
            {searching ? t("docs.link.searching") : t("docs.link.search")}
          </Button>
        </div>
        <span className="hint">{t("docs.link.help")}</span>
        {searchError ? <span className="err" role="alert">{searchError}</span> : null}
      </form>

      {rows && rows.length === 0 ? <span className="hint" role="status">{t("docs.link.none")}</span> : null}

      {rows && rows.length > 0 ? (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <Button size="sm" variant="ghost" onClick={markAll} disabled={downloading}>{t("docs.link.markAll")}</Button>
            <Button size="sm" variant="ghost" onClick={clearMarks} disabled={downloading}>{t("docs.link.clear")}</Button>
            <span style={{ flex: 1 }} />
            <Button size="sm" variant="primary" onClick={() => void download()} disabled={downloading || marked === 0}>
              {t("docs.link.download", { n: marked })}
            </Button>
          </div>
          <span className="hint">{t("docs.link.limit", { n: MAX_MARKED_LINKS })}</span>
          <ul aria-label={t("docs.link.list")} style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 6 }}>
            {rows.map((row, index) => (
              <li key={row.item.url} style={{ display: "flex", flexDirection: "column", gap: 4, padding: "8px 10px", border: "1px solid var(--border)", borderRadius: 10 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <input
                    type="checkbox"
                    aria-label={t("docs.link.select", { name: row.item.title })}
                    checked={row.marked}
                    disabled={downloading || row.state.phase === "done"}
                    onChange={() => toggle(index)}
                    style={{ accentColor: "var(--acc)", flexShrink: 0 }}
                  />
                  <Icon name="file" size={14} />
                  <span title={row.item.title} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1, fontSize: 13 }}>{row.item.title}</span>
                  {row.state.phase === "waiting" ? <Pill>{t("docs.link.state.waiting")}</Pill> : null}
                  {row.state.phase === "running" ? <Pill tone="acc" aria-busy="true">{t("docs.link.state.running")}</Pill> : null}
                  {row.state.phase === "done" ? <Pill tone="ok">{t("docs.link.state.done")}</Pill> : null}
                  {row.state.phase === "failed" ? <Pill tone="bad">{t("docs.link.state.failed")}</Pill> : null}
                </div>
                <div className="mono" style={{ display: "flex", gap: 10, flexWrap: "wrap", fontSize: 10, color: "var(--t3)" }}>
                  <span>{row.item.kind.toUpperCase()}</span>
                  <span>{row.item.host}</span>
                </div>
                {row.state.phase === "failed" ? (
                  <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                    <span className="err" role="alert" style={{ flex: 1, minWidth: 0 }}>{reasonText(row.state.reason)}</span>
                    {row.state.reason === "blocked" ? (
                      <a className="btn sm" href={row.item.url} target="_blank" rel="noopener noreferrer" aria-label={t("docs.link.openNamed", { name: row.item.title })}>
                        {t("docs.link.open")}
                      </a>
                    ) : null}
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </section>
  );
}
