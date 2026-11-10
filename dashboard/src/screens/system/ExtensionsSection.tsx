import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api, ApiError, useApiQuery } from "@/api";
import { Button, EmptyState, ErrorState, Field, Glyph, Input, Loading, Modal, Pane, Pill, Row, Toggle } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { normalizeRepo, repoProblem } from "./linkFormats";
import { readTab, storeTab } from "./extTab";
import type { ExtTab } from "./extTab";
import { RemoteServers } from "./RemoteServers";
import type { AddResult, ExtensionRequest, ExtensionsData, MarketData, MarketItem, MarketplaceSource, OfferedPlugin, PluginItem, PluginState } from "./types";

type PendingAction = "toggle" | "install" | "uninstall" | "removeMarket";

interface Pending {
  action: PendingAction;
  id: string;
  name: string;
  enabled: boolean;
  /** What turning it on does, as the CLI shows it. Empty for plugins from the marketplace list. */
  summary: string;
}

const MARKETPLACE_NAME = "claude-plugins-official";

function errorText(cause: unknown, fallback: string): string {
  return cause instanceof ApiError || cause instanceof Error ? cause.message : fallback;
}

/** Settings > Extensions: what is installed, requests from agents, the Anthropic marketplace and "add by link". */
export function ExtensionsSection() {
  const { t } = useI18n();
  const shell = useShell();
  const list = useApiQuery<ExtensionsData>("/api/extensions");
  const [pending, setPending] = useState<Pending | null>(null);
  const [tab, setTab] = useState<ExtTab>(readTab);
  const [linking, setLinking] = useState(false);
  const [openMarket, setOpenMarket] = useState<string | null>(null);
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

  const confirmPending = () =>
    pending &&
    run(async () => {
      if (pending.action === "removeMarket") {
        await api.delete(`/api/extensions/marketplaces/${encodeURIComponent(pending.id)}`);
        shell.toast({ tone: "ok", text: t("ext.changed.marketRemoved", { name: pending.name }) });
      } else if (pending.action === "uninstall") {
        await api.post("/api/extensions/uninstall", { id: pending.id, confirmed: true });
        shell.toast({ tone: "ok", text: t("ext.changed.uninstalled", { name: pending.name }) });
      } else {
        // Installing is one step for the owner: the marketplace joins the list first when it is not there yet.
        if (pending.action === "install" && pending.id.endsWith(`@${MARKETPLACE_NAME}`) && !list.data?.marketplaces.some((m) => m.name === MARKETPLACE_NAME)) {
          await api.post("/api/extensions/marketplace");
        }
        await api.post("/api/extensions/toggle", { id: pending.id, enabled: pending.enabled, confirmed: true });
        shell.toast({ tone: "ok", text: t(pending.action === "install" ? "ext.changed.installed" : pending.enabled ? "ext.changed.on" : "ext.changed.off", { name: pending.name }) });
      }
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

  const chooseTab = (next: ExtTab) => {
    setTab(next);
    storeTab(next);
  };

  if (list.loading && !list.data) return <Loading />;
  if (list.error || !list.data) return <ErrorState message={list.error?.message} onRetry={list.reload} />;
  const data = list.data;

  const requestsOf = (mcp: boolean) => data.requests.filter((r) => (r.kind === "mcp") === mcp);
  const localServers = data.servers.filter((s) => !s.remote);
  const askToggle = (plugin: PluginItem, enabled: boolean) => setPending({ action: "toggle", id: plugin.id, name: plugin.name, enabled, summary: plugin.summary });
  const askUninstall = (id: string, name: string) => setPending({ action: "uninstall", id, name, enabled: false, summary: "" });

  const requestsBlock = (mcp: boolean) =>
    requestsOf(mcp).length > 0 ? (
      <section style={{ display: "flex", flexDirection: "column", gap: 8 }} aria-label={t("ext.requests")}>
        <span className="lbl">{t("ext.requests")}</span>
        {requestsOf(mcp).map((request) => (
          <RequestCard key={request.id} request={request} busy={busy || !data.database} onDecide={(verdict) => void decide(request, verdict)} />
        ))}
      </section>
    ) : null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div className="ext-tabs" role="tablist" aria-label={t("ext.tabs")}>
        {(["plugins", "mcp"] as const).map((id) => (
          <button key={id} type="button" role="tab" aria-selected={tab === id} className="ext-tab" onClick={() => chooseTab(id)}>
            {t(id === "plugins" ? "ext.tab.plugins" : "ext.tab.mcp")}
          </button>
        ))}
      </div>

      {!data.database ? <p className="p" role="note">{t("ext.noDatabase")}</p> : null}
      {error ? <p className="p" role="alert" style={{ color: "var(--bad)" }}>{error}</p> : null}

      <div role="tabpanel" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {tab === "plugins" ? (
          <>
            <span className="lbl">{t("ext.installed")}</span>
            {data.plugins.length === 0 ? (
              <Pane>
                <EmptyState icon="extensions" title={t("ext.empty.title")} description={t("ext.empty.body")} />
              </Pane>
            ) : (
              <Pane style={{ padding: "4px 0" }}>
                {data.plugins.map((plugin) => (
                  <PluginRow key={plugin.id} plugin={plugin} disabled={busy || !data.database} onToggle={(enabled) => askToggle(plugin, enabled)} onUninstall={() => askUninstall(plugin.id, plugin.name)} />
                ))}
              </Pane>
            )}

            {requestsBlock(false)}

            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <Button size="sm" variant="ghost" onClick={() => setLinking((open) => !open)} aria-pressed={linking}>
                {t("ext.add")}
              </Button>
            </div>

            {linking ? (
              <AddByLink
                onDone={(result) => {
                  setLinking(false);
                  setOpenMarket(result.kind === "marketplace" && result.installed.length === 0 ? result.name : null);
                  list.reload();
                  shell.emit("extensions:changed");
                }}
              />
            ) : null}

            {data.marketplaces.length > 0 ? (
              <section style={{ display: "flex", flexDirection: "column", gap: 8 }} aria-label={t("ext.sources")}>
                <span className="lbl">{t("ext.sources")}</span>
                {data.marketplaces.map((source) => (
                  <MarketplaceEntry
                    key={source.name}
                    source={source}
                    open={openMarket === source.name}
                    onOpen={(open) => setOpenMarket(open ? source.name : null)}
                    disabled={busy || !data.database}
                    onInstall={(item) => setPending({ action: "install", id: item.id, name: item.display_name, enabled: true, summary: "" })}
                    onUninstall={(item) => askUninstall(item.id, item.display_name)}
                    onRemove={() => setPending({ action: "removeMarket", id: source.name, name: source.name, enabled: false, summary: String(source.plugins ?? 0) })}
                    refreshKey={data.plugins.map((p) => `${p.id}:${p.enabled}`).join("|")}
                  />
                ))}
              </section>
            ) : null}

            <MarketplaceList
              disabled={busy || !data.database}
              onInstall={(item) => setPending({ action: "install", id: item.id, name: item.display_name, enabled: true, summary: "" })}
              onToggle={(item, enabled) => setPending({ action: "toggle", id: item.id, name: item.display_name, enabled, summary: "" })}
              onUninstall={(item) => askUninstall(item.id, item.display_name)}
              refreshKey={data.plugins.map((p) => `${p.id}:${p.enabled}`).join("|")}
            />
          </>
        ) : (
          <>
            {requestsBlock(true)}

            <RemoteServers onChanged={() => { list.reload(); shell.emit("extensions:changed"); }} />

            <span className="lbl">{t("ext.servers.local")}</span>
            {localServers.length === 0 ? (
              <p className="p">{t("ext.servers.none")}</p>
            ) : (
              <Pane style={{ padding: "4px 0" }}>
                {localServers.map((server) => (
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
          </>
        )}

        {data.problems.length > 0 ? (
          <>
            <span className="lbl">{t("ext.problems")}</span>
            {data.problems.map((problem) => (
              <p key={problem} className="p" style={{ fontSize: 12 }}>{problem}</p>
            ))}
          </>
        ) : null}
      </div>

      {pending ? (
        <Modal label={t(`ext.confirm.${pending.action === "toggle" ? (pending.enabled ? "enable" : "disable") : pending.action}`, { name: pending.name })} onClose={() => setPending(null)} style={{ flexDirection: "column", width: "min(560px, 100%)", padding: 22, gap: 14 }}>
          <h2 className="h1" style={{ fontSize: 17 }}>{t(`ext.confirm.${pending.action === "toggle" ? (pending.enabled ? "enable" : "disable") : pending.action}`, { name: pending.name })}</h2>
          <p className="p">{t(`ext.confirm.${pending.action === "toggle" ? (pending.enabled ? "enable" : "disable") : pending.action}Body`, { count: pending.action === "removeMarket" ? pending.summary : "" })}</p>
          {pending.enabled ? (
            <pre className="mono" style={{ margin: 0, fontSize: 12, lineHeight: 1.7, color: "var(--t1)", whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
              {pending.summary || pending.id}
            </pre>
          ) : null}
          {error ? <p className="p" role="alert" style={{ color: "var(--bad)" }}>{error}</p> : null}
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <Button variant="ghost" onClick={() => setPending(null)}>{t("ext.confirm.cancel")}</Button>
            <Button variant="primary" disabled={busy} onClick={() => void confirmPending()}>{t("ext.confirm.ok")}</Button>
          </div>
        </Modal>
      ) : null}
    </div>
  );
}

const STATE_TONE: Record<PluginState, "ok" | "warn" | "bad" | "default"> = { active: "ok", pending: "warn", error: "bad", disabled: "default" };

function PluginRow({ plugin, disabled, onToggle, onUninstall }: { plugin: PluginItem; disabled: boolean; onToggle: (enabled: boolean) => void; onUninstall: () => void }) {
  const { t } = useI18n();
  return (
    <Row>
      <Glyph initial={plugin.name.slice(0, 1).toUpperCase()} size={34} />
      <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
        <span className="h2">{plugin.name}</span>
        {plugin.reason ? <span className="p" style={{ fontSize: 12, overflowWrap: "anywhere" }}>{plugin.reason}</span> : null}
      </div>
      <Pill>{plugin.marketplace}</Pill>
      <Pill tone={STATE_TONE[plugin.state]} aria-label={t("ext.state.of", { name: plugin.name })}>{t(`ext.state.${plugin.state}`)}</Pill>
      <Toggle checked={plugin.enabled} disabled={disabled} label={t("ext.toggle", { name: plugin.name })} onChange={onToggle} />
      <Button size="sm" variant="ghost" disabled={disabled} aria-label={t("ext.uninstall.of", { name: plugin.name })} onClick={onUninstall}>{t("ext.uninstall")}</Button>
    </Row>
  );
}

function MarketplaceEntry({ source, open, onOpen, disabled, onInstall, onUninstall, onRemove, refreshKey }: { source: MarketplaceSource; open: boolean; onOpen: (open: boolean) => void; disabled: boolean; onInstall: (item: OfferedPlugin) => void; onUninstall: (item: OfferedPlugin) => void; onRemove: () => void; refreshKey: string }) {
  const { t } = useI18n();
  return (
    <Pane style={{ padding: 12, display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <Pill>{source.name}</Pill>
        <span className="p mono" style={{ fontSize: 12, flex: 1, overflowWrap: "anywhere" }}>{source.source}</span>
        <Button size="sm" variant="ghost" aria-expanded={open} aria-label={t("ext.market.plugins.of", { name: source.name })} onClick={() => onOpen(!open)}>
          {t(open ? "ext.market.hide" : "ext.market.show")}
        </Button>
        <Button size="sm" variant="ghost" disabled={disabled} aria-label={t("ext.market.remove.of", { name: source.name })} onClick={onRemove}>{t("ext.market.remove")}</Button>
      </div>
      {open ? <OfferedPlugins name={source.name} disabled={disabled} onInstall={onInstall} onUninstall={onUninstall} refreshKey={refreshKey} /> : null}
    </Pane>
  );
}

function OfferedPlugins({ name, disabled, onInstall, onUninstall, refreshKey }: { name: string; disabled: boolean; onInstall: (item: OfferedPlugin) => void; onUninstall: (item: OfferedPlugin) => void; refreshKey: string }) {
  const { t } = useI18n();
  const result = useApiQuery<{ plugins: OfferedPlugin[] }>(`/api/extensions/marketplaces/${encodeURIComponent(name)}/plugins?r=${encodeURIComponent(refreshKey)}`);
  if (result.loading && !result.data) return <Loading inline />;
  if (result.error || !result.data) return <ErrorState message={result.error?.message} onRetry={result.reload} />;
  if (result.data.plugins.length === 0) return <p className="p">{t("ext.market.none")}</p>;
  return (
    <div style={{ borderTop: "1px solid var(--border)" }}>
      {result.data.plugins.map((item) => (
        <Row key={item.id}>
          <Glyph initial={item.display_name.slice(0, 1).toUpperCase()} size={34} />
          <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
            <span className="h2">{item.display_name}</span>
            <span className="p" style={{ fontSize: 12 }}>{item.description}</span>
          </div>
          {item.installed ? (
            <>
              <Pill tone={item.enabled ? "ok" : "default"}>{t(item.enabled ? "ext.market.installedOn" : "ext.market.installedOff")}</Pill>
              <Button size="sm" variant="ghost" disabled={disabled} aria-label={t("ext.uninstall.of", { name: item.display_name })} onClick={() => onUninstall(item)}>{t("ext.uninstall")}</Button>
            </>
          ) : (
            <Button size="sm" variant="primary" disabled={disabled} aria-label={t("ext.install.of", { name: item.display_name })} onClick={() => onInstall(item)}>{t("ext.install")}</Button>
          )}
        </Row>
      ))}
    </div>
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

function AddByLink({ onDone }: { onDone: (result: AddResult) => void }) {
  const { t } = useI18n();
  const shell = useShell();
  const [repo, setRepo] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const problem = repoProblem(repo);
  const valid = problem === null;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!valid) return;
    setBusy(true);
    setError(null);
    try {
      const added = await api.post<AddResult>("/api/extensions/marketplaces", { repo: normalizeRepo(repo) });
      const installed = added.installed[0]?.split("@")[0];
      shell.toast({ tone: "ok", text: installed ? t("ext.link.installed", { name: installed }) : added.plugins.length > 1 ? t("ext.link.choose", { name: added.name, count: added.plugins.length }) : t("ext.link.added", { name: added.name }) });
      onDone(added);
    } catch (cause) {
      setError(errorText(cause, t("api.error")));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Pane style={{ padding: 14 }}>
      <form onSubmit={(event) => void submit(event)} style={{ display: "flex", flexDirection: "column", gap: 10 }} aria-label={t("ext.link.title")}>
        <Field label={t("ext.link.label")} hint={t("ext.link.hint")} error={error ?? (problem === "format" ? t("ext.link.badFormat") : undefined)}>
          {(control) => <Input {...control} value={repo} onChange={(event) => setRepo(event.target.value)} placeholder="owner/repo" autoComplete="off" spellCheck={false} />}
        </Field>
        <div>
          <Button type="submit" size="sm" variant="primary" disabled={!valid || busy}>{t("ext.link.submit")}</Button>
          {problem === "empty" ? <span className="hint" style={{ marginLeft: 10 }}>{t("ext.link.empty")}</span> : null}
        </div>
      </form>
    </Pane>
  );
}

function MarketplaceList({ disabled, onInstall, onToggle, onUninstall, refreshKey }: { disabled: boolean; onInstall: (item: MarketItem) => void; onToggle: (item: MarketItem, enabled: boolean) => void; onUninstall: (item: MarketItem) => void; refreshKey: string }) {
  const { t } = useI18n();
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(query.trim()), 250);
    return () => clearTimeout(timer);
  }, [query]);

  const result = useApiQuery<MarketData>(`/api/extensions/marketplace?q=${encodeURIComponent(debounced)}&r=${encodeURIComponent(refreshKey)}`);
  const data = result.data;

  return (
    <Pane style={{ padding: 14, display: "flex", flexDirection: "column", gap: 10 }} aria-label={t("ext.market.title")} as="section">
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <span className="h2" style={{ flex: 1 }}>{t("ext.market.title")}</span>
      </div>
      <Input type="search" aria-label={t("ext.market.search")} placeholder={t("ext.market.search")} value={query} onChange={(event) => setQuery(event.target.value)} />
      {result.loading && !data ? <Loading inline /> : null}
      {result.error ? <ErrorState message={result.error.message} onRetry={result.reload} /> : null}
      {data ? (
        <>
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
                  {item.installed ? (
                    <>
                      <Toggle checked={item.enabled} disabled={disabled} label={t("ext.toggle", { name: item.display_name })} onChange={(next) => onToggle(item, next)} />
                      <Button size="sm" variant="ghost" disabled={disabled} aria-label={t("ext.uninstall.of", { name: item.display_name })} onClick={() => onUninstall(item)}>{t("ext.uninstall")}</Button>
                    </>
                  ) : (
                    <Button size="sm" variant="primary" disabled={disabled} aria-label={t("ext.install.of", { name: item.display_name })} onClick={() => onInstall(item)}>{t("ext.install")}</Button>
                  )}
                </Row>
              ))}
            </div>
          )}
        </>
      ) : null}
    </Pane>
  );
}
