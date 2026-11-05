import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, ApiError, useApiQuery } from "@/api";
import { Button, EmptyState, ErrorState, Field, Glyph, Input, Loading, Modal, Pane, Pill, Row, Toggle } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import type { ExtensionRequest, ExtensionsData, MarketData, MarketItem, PluginItem } from "./types";

interface Pending {
  id: string;
  name: string;
  enabled: boolean;
  /** What turning it on does, as the CLI shows it. Empty for plugins from the marketplace list. */
  summary: string;
}

const REPO_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

function errorText(cause: unknown, fallback: string): string {
  return cause instanceof ApiError || cause instanceof Error ? cause.message : fallback;
}

/** Settings > Extensions: what is installed, requests from agents, the Anthropic marketplace and "add by link". */
export function ExtensionsSection() {
  const { t } = useI18n();
  const shell = useShell();
  const list = useApiQuery<ExtensionsData>("/api/extensions");
  const [pending, setPending] = useState<Pending | null>(null);
  const [market, setMarket] = useState(false);
  const [linking, setLinking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (cause) {
      setError(errorText(cause, t("api.error")));
    } finally {
      setBusy(false);
    }
  };

  const confirmToggle = () =>
    pending &&
    run(async () => {
      await api.post("/api/extensions/toggle", { id: pending.id, enabled: pending.enabled, confirmed: true });
      shell.toast({ tone: "ok", text: t(pending.enabled ? "ext.changed.on" : "ext.changed.off", { name: pending.name }) });
      setPending(null);
      list.reload();
      shell.emit("extensions:changed");
    });

  const decide = (request: ExtensionRequest, verdict: "approve" | "reject") =>
    run(async () => {
      await api.post(`/api/extensions/requests/${request.id}/${verdict}`);
      shell.toast({ tone: "ok", text: t(verdict === "approve" ? "ext.requests.approved" : "ext.requests.rejected") });
      list.reload();
      shell.emit("extensions:changed");
    });

  if (list.loading && !list.data) return <Loading />;
  if (list.error || !list.data) return <ErrorState message={list.error?.message} onRetry={list.reload} />;
  const data = list.data;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <Button size="sm" onClick={() => setMarket((open) => !open)} aria-pressed={market}>
          {t("ext.marketplace")}
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setLinking((open) => !open)} aria-pressed={linking}>
          {t("ext.add")}
        </Button>
      </div>

      {!data.database ? <p className="p" role="note">{t("ext.noDatabase")}</p> : null}
      {error ? <p className="p" role="alert" style={{ color: "var(--bad)" }}>{error}</p> : null}

      {linking ? <AddByLink onDone={() => { setLinking(false); list.reload(); }} /> : null}

      {market ? (
        <MarketplaceList
          disabled={!data.database}
          onToggle={(item, enabled) => setPending({ id: item.id, name: item.display_name, enabled, summary: "" })}
          onAdded={list.reload}
          refreshKey={data.plugins.map((p) => `${p.id}:${p.enabled}`).join("|")}
        />
      ) : null}

      {data.requests.length > 0 ? (
        <section style={{ display: "flex", flexDirection: "column", gap: 8 }} aria-label={t("ext.requests")}>
          <span className="lbl">{t("ext.requests")}</span>
          {data.requests.map((request) => (
            <RequestCard key={request.id} request={request} busy={busy || !data.database} onDecide={(verdict) => void decide(request, verdict)} />
          ))}
        </section>
      ) : null}

      <span className="lbl">{t("ext.installed")}</span>
      {data.plugins.length + data.servers.length === 0 ? (
        <Pane>
          <EmptyState icon="extensions" title={t("ext.empty.title")} description={t("ext.empty.body")} />
        </Pane>
      ) : (
        <Pane style={{ padding: "4px 0" }}>
          {data.plugins.map((plugin) => (
            <PluginRow key={plugin.id} plugin={plugin} disabled={busy || !data.database} onToggle={(enabled) => setPending({ id: plugin.id, name: plugin.name, enabled, summary: plugin.summary })} />
          ))}
          {data.servers.map((server) => (
            <Row key={server.id}>
              <Glyph initial={server.name.slice(0, 1).toUpperCase()} size={34} />
              <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
                <span className="h2">{server.name}</span>
                <span className="p mono" style={{ fontSize: 12, overflowWrap: "anywhere" }}>{server.target}</span>
              </div>
              <Pill>{t("ext.server")}</Pill>
              <Pill tone="ok">{t("ext.server.on")}</Pill>
            </Row>
          ))}
        </Pane>
      )}

      {data.marketplaces.length > 0 ? (
        <>
          <span className="lbl">{t("ext.sources")}</span>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {data.marketplaces.map((source) => (
              <div key={source.name} style={{ display: "flex", gap: 10, alignItems: "baseline" }}>
                <Pill>{source.name}</Pill>
                <span className="p mono" style={{ fontSize: 12 }}>{source.source}</span>
              </div>
            ))}
          </div>
        </>
      ) : null}

      {data.problems.length > 0 ? (
        <>
          <span className="lbl">{t("ext.problems")}</span>
          {data.problems.map((problem) => (
            <p key={problem} className="p" style={{ fontSize: 12 }}>{problem}</p>
          ))}
        </>
      ) : null}

      {pending ? (
        <Modal label={t(pending.enabled ? "ext.confirm.enable" : "ext.confirm.disable", { name: pending.name })} onClose={() => setPending(null)} style={{ flexDirection: "column", width: "min(560px, 100%)", padding: 22, gap: 14 }}>
          <h2 className="h1" style={{ fontSize: 17 }}>{t(pending.enabled ? "ext.confirm.enable" : "ext.confirm.disable", { name: pending.name })}</h2>
          <p className="p">{t(pending.enabled ? "ext.confirm.enableBody" : "ext.confirm.disableBody")}</p>
          {pending.enabled ? (
            <pre className="mono" style={{ margin: 0, fontSize: 12, lineHeight: 1.7, color: "var(--t1)", whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
              {pending.summary || pending.id}
            </pre>
          ) : null}
          {error ? <p className="p" role="alert" style={{ color: "var(--bad)" }}>{error}</p> : null}
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <Button variant="ghost" onClick={() => setPending(null)}>{t("ext.confirm.cancel")}</Button>
            <Button variant="primary" disabled={busy} onClick={() => void confirmToggle()}>{t("ext.confirm.ok")}</Button>
          </div>
        </Modal>
      ) : null}
    </div>
  );
}

function PluginRow({ plugin, disabled, onToggle }: { plugin: PluginItem; disabled: boolean; onToggle: (enabled: boolean) => void }) {
  const { t } = useI18n();
  return (
    <Row>
      <Glyph initial={plugin.name.slice(0, 1).toUpperCase()} size={34} />
      <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
        <span className="h2">{plugin.name}</span>
      </div>
      <Pill>{plugin.marketplace}</Pill>
      <Toggle checked={plugin.enabled} disabled={disabled} label={t("ext.toggle", { name: plugin.name })} onChange={onToggle} />
    </Row>
  );
}

function RequestCard({ request, busy, onDecide }: { request: ExtensionRequest; busy: boolean; onDecide: (verdict: "approve" | "reject") => void }) {
  const { t } = useI18n();
  return (
    <Pane style={{ padding: 14, display: "flex", flexDirection: "column", gap: 10 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <Pill tone="org">{request.kind === "mcp" ? t("ext.server") : request.kind}</Pill>
        <span className="h2">{t("ext.requests.from", { name: request.name })}</span>
        <span className="mono" style={{ fontSize: 11, color: "var(--t3)" }}>{request.requested_at}</span>
      </div>
      {request.reason ? <p className="p">{request.reason}</p> : null}
      <pre className="mono" style={{ margin: 0, fontSize: 12, lineHeight: 1.7, color: "var(--t1)", whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{request.summary}</pre>
      {request.risks.length > 0 ? (
        <div>
          <span className="lbl">{t("ext.requests.risks")}</span>
          <ul className="p" style={{ margin: "4px 0 0", paddingLeft: 18 }}>
            {request.risks.map((risk) => (
              <li key={risk}>{risk}</li>
            ))}
          </ul>
        </div>
      ) : null}
      <div style={{ display: "flex", gap: 8 }}>
        <Button size="sm" variant="primary" disabled={busy} onClick={() => onDecide("approve")}>{t("ext.requests.approve")}</Button>
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => onDecide("reject")}>{t("ext.requests.reject")}</Button>
      </div>
    </Pane>
  );
}

function AddByLink({ onDone }: { onDone: () => void }) {
  const { t } = useI18n();
  const shell = useShell();
  const [repo, setRepo] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const valid = REPO_PATTERN.test(repo.trim());

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!valid) return;
    setBusy(true);
    setError(null);
    try {
      const added = await api.post<{ name: string }>("/api/extensions/marketplaces", { repo: repo.trim() });
      shell.toast({ tone: "ok", text: t("ext.link.added", { name: added.name }) });
      onDone();
    } catch (cause) {
      setError(errorText(cause, t("api.error")));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Pane style={{ padding: 14 }}>
      <form onSubmit={(event) => void submit(event)} style={{ display: "flex", flexDirection: "column", gap: 10 }} aria-label={t("ext.link.title")}>
        <Field label={t("ext.link.label")} hint={t("ext.link.hint")} error={error ?? undefined}>
          {(control) => <Input {...control} value={repo} onChange={(event) => setRepo(event.target.value)} placeholder="owner/repo" autoComplete="off" spellCheck={false} />}
        </Field>
        <div>
          <Button type="submit" size="sm" variant="primary" disabled={!valid || busy}>{t("ext.link.submit")}</Button>
        </div>
      </form>
    </Pane>
  );
}

function MarketplaceList({ disabled, onToggle, onAdded, refreshKey }: { disabled: boolean; onToggle: (item: MarketItem, enabled: boolean) => void; onAdded: () => void; refreshKey: string }) {
  const { t } = useI18n();
  const shell = useShell();
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(query.trim()), 250);
    return () => clearTimeout(timer);
  }, [query]);

  const result = useApiQuery<MarketData>(`/api/extensions/marketplace?q=${encodeURIComponent(debounced)}&r=${encodeURIComponent(refreshKey)}`);
  const data = result.data;

  const add = async () => {
    setAdding(true);
    setAddError(null);
    try {
      await api.post("/api/extensions/marketplace");
      shell.toast({ tone: "ok", text: t("ext.market.added") });
      result.reload();
      onAdded();
    } catch (cause) {
      setAddError(errorText(cause, t("api.error")));
    } finally {
      setAdding(false);
    }
  };

  return (
    <Pane style={{ padding: 14, display: "flex", flexDirection: "column", gap: 10 }} aria-label={t("ext.market.title")} as="section">
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <span className="h2" style={{ flex: 1 }}>{t("ext.market.title")}</span>
        {data && !data.added ? (
          <Button size="sm" variant="primary" disabled={adding || disabled} onClick={() => void add()}>{t("ext.market.addToList")}</Button>
        ) : null}
      </div>
      {addError ? <p className="p" role="alert" style={{ color: "var(--bad)" }}>{addError}</p> : null}
      <Input type="search" aria-label={t("ext.market.search")} placeholder={t("ext.market.search")} value={query} onChange={(event) => setQuery(event.target.value)} />
      {result.loading && !data ? <Loading inline /> : null}
      {result.error ? <ErrorState message={result.error.message} onRetry={result.reload} /> : null}
      {data ? (
        <>
          {!data.added ? <p className="p" style={{ fontSize: 12 }}>{t("ext.market.needsAdd")}</p> : null}
          <span className="mono" style={{ fontSize: 11, color: "var(--t3)" }}>{t("ext.market.count", { shown: data.items.length, total: data.total })}</span>
          {data.items.length === 0 ? (
            <p className="p">{t("ext.market.none")}</p>
          ) : (
            <div style={{ maxHeight: 320, overflow: "auto", borderTop: "1px solid var(--border)" }}>
              {data.items.map((item) => (
                <Row key={item.id}>
                  <Glyph initial={item.display_name.slice(0, 1).toUpperCase()} size={34} />
                  <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
                    <span className="h2">{item.display_name}</span>
                    <span className="p" style={{ fontSize: 12 }}>{item.description}</span>
                  </div>
                  {item.category ? <Pill>{item.category}</Pill> : null}
                  <Toggle checked={item.enabled} disabled={disabled || !data.added} label={t("ext.toggle", { name: item.display_name })} onChange={(next) => onToggle(item, next)} />
                </Row>
              ))}
            </div>
          )}
        </>
      ) : null}
    </Pane>
  );
}
