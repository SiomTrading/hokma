import { useCallback, useEffect, useState } from "react";
import { api, ApiError, useApiQuery } from "@/api";
import { announceRestore } from "@/api/restart";
import { Button, ErrorState, Field, Input, Loading, Pane, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { BusyConfirmDialog } from "../system/BusyConfirmDialog";
import { formatWhen } from "../permissions/format";
import { formatSize } from "../projects/format";

export interface BackupState {
  connected: boolean;
  repo: string | null;
  last_backup_at: string | null;
  last_size: number | null;
  status: "idle" | "running" | "ok" | "warning" | "error";
  message: string | null;
  /** Catalog key of why the last backup failed, when the panel can act on it (for example `github.not_connected`). */
  code?: string | null;
  total_size_estimate: number;
  omitted: { path: string; reason: string }[];
  key_ready: boolean;
  /** Set when a restore could not be applied at start-up; stays until the student dismisses it. */
  restore_failed?: { at: string } | null;
}
interface DeviceCode { user_code: string; verification_uri: string; interval: number; expires_in?: number }
interface GithubState {
  connected: boolean;
  user: string | null;
  /** "owner/name" the student picked; Hokma only sees this repository. */
  repo?: string | null;
  /** An old all-repositories connection was dropped: the student has to connect again and pick a repository. */
  legacy?: boolean;
  offline?: boolean;
  pending?: DeviceCode | null;
  install_url?: string;
  new_repo_url?: string;
  legacy_url?: string;
}
interface RepoItem { full_name: string; private: boolean }
interface Reconnect { kind: "engine" | "telegram" | "folder" | "projects"; name: string }
interface BackupEntry { name: string; size: number; created_at: string }

const GIB = 1024 ** 3;
export const sizeLabel = (bytes: number) => (bytes >= GIB ? `${(bytes / GIB).toFixed(1)} GB` : formatSize(bytes));
const TONE = { idle: undefined, running: "warn", ok: "ok", warning: "warn", error: "bad" } as const;
/** Reasons the engine gives for a path that is not in the backup; any other value is shown as the path alone. */
const OMITTED_REASONS = ["secret", "git", "too_large"];
/** A polling that fails this many times in a row is given up (with `onError`). */
const MAX_POLL_FAILURES = 3;
const stack = { display: "flex", flexDirection: "column", gap: 12 } as const;
const inline = { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" } as const;

/**
 * Runs `fn` every `seconds` while `active`; stops when `fn` returns true. A failed tick is tried again; with `onError`,
 * the third failure in a row (or a 503, which will not fix itself) stops the polling and reports the error.
 */
function usePoll(active: boolean, seconds: number, fn: () => Promise<boolean>, onError?: (cause: unknown) => void) {
  useEffect(() => {
    if (!active) return;
    let stopped = false;
    let failures = 0;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      let done = false;
      try {
        done = await fn();
        failures = 0;
      } catch (cause) {
        failures += 1;
        if (onError && (failures >= MAX_POLL_FAILURES || (cause instanceof ApiError && cause.status === 503))) {
          if (!stopped) onError(cause);
          return;
        }
      }
      if (!stopped && !done) timer = setTimeout(() => void tick(), seconds * 1000);
    };
    timer = setTimeout(() => void tick(), seconds * 1000);
    return () => { stopped = true; clearTimeout(timer); };
  }, [active, seconds, fn, onError]);
}

/** Settings > Backup and restore: connect GitHub, keep the recovery key, back up now, restore on this computer. */
export function BackupSection() {
  const { t, locale } = useI18n();
  const backup = useApiQuery<BackupState>("/api/backup");
  const github = useApiQuery<GithubState>("/api/github");
  const [state, setState] = useState<BackupState | null>(null);
  const [gh, setGh] = useState<GithubState | null>(null);
  const [device, setDevice] = useState<DeviceCode | null>(null);
  const [repos, setRepos] = useState<RepoItem[] | null>(null);
  const [newKey, setNewKey] = useState<string | null>(null);
  const [last4, setLast4] = useState("");
  const [entries, setEntries] = useState<BackupEntry[] | null>(null);
  const [picked, setPicked] = useState("");
  const [restoreKey, setRestoreKey] = useState("");
  const [reconnect, setReconnect] = useState<Reconnect[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [askBusy, setAskBusy] = useState(false); // an agent is working: ask before the restore restarts the engine

  const current = state ?? backup.data;
  const user = (gh ?? github.data)?.user ?? null;
  const connected = (gh ?? github.data)?.connected ?? false;
  const repo = (gh ?? github.data)?.repo ?? null;
  const picking = connected && repo === null;                  // connected, but no repository chosen yet
  const links = github.data;
  const offline = !gh && (github.data?.offline ?? false);

  const refresh = useCallback(async () => {
    const next = await api.get<BackupState>("/api/backup");
    setState(next);
    return next.status !== "running";
  }, []);
  usePoll(current?.status === "running", 2, refresh);

  const reloadGithub = github.reload;
  // The engine drops the token when GitHub rejects it: the "Connected as" pill must not outlive it.
  useEffect(() => {
    if (current?.status === "error" && (current.code === "github.not_connected" || current.code === "github.repo_gone")) {
      setGh(null);
      reloadGithub();
    }
  }, [current?.status, current?.code, reloadGithub]);
  // A reload in the middle of the connection: the engine still waits for the code, so show it again.
  useEffect(() => {
    const pending = github.data?.pending;
    if (pending && !github.data?.connected) setDevice(pending);
  }, [github.data]);

  const check = useCallback(async () => {
    const res = await api.get<{ status: string; user?: string }>("/api/github/connect");
    if (res.status === "connected") {
      setGh({ connected: true, user: res.user ?? null, repo: null });
      setDevice(null);
      void refresh();
      return true;
    }
    if (res.status === "expired" || res.status === "denied") {
      setDevice(null);
      setError(t(`backup.github.${res.status}`));
      return true;
    }
    return false;
  }, [refresh, t]);
  const checkFailed = useCallback((cause: unknown) => {
    setDevice(null);                                        // back to "Connect GitHub", which is the retry
    setError(cause instanceof ApiError || cause instanceof Error ? cause.message : t("api.error"));
  }, [t]);
  usePoll(device !== null && !connected, device?.interval ?? 5, check, checkFailed);

  // The repository list: loaded when the picker opens, and every 4 seconds until GitHub shows at least one (the student
  // is installing the App in another window).
  const loadRepos = useCallback(async () => {
    const list = await api.get<RepoItem[]>("/api/github/repos");
    setRepos(list);
    return list.length > 0;
  }, []);
  useEffect(() => {
    if (picking) void loadRepos().catch(() => setRepos([]));
    else setRepos(null);
  }, [picking, loadRepos]);
  const repoPollFailed = useCallback((cause: unknown) => {
    setError(cause instanceof ApiError || cause instanceof Error ? cause.message : t("api.error"));
  }, [t]);
  usePoll(picking && (repos === null || repos.length === 0), 4, loadRepos, repoPollFailed);

  if ((backup.loading && !backup.data) || (github.loading && !github.data)) return <Loading />;
  if (backup.error || !current) return <ErrorState message={backup.error?.message} onRetry={backup.reload} />;

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (cause) {
      setError(cause instanceof ApiError || cause instanceof Error ? cause.message : t("api.error"));
    } finally {
      setBusy(false);
    }
  };

  const startConnect = () => run(async () => setDevice(await api.post<DeviceCode>("/api/github/connect")));
  const disconnect = () =>
    run(async () => {
      await api.delete("/api/github");
      setGh({ connected: false, user: null, repo: null });
      setDevice(null);
      await refresh();                                      // an error that only said "connect GitHub" is gone
    });
  const pickRepo = (fullName: string) =>
    run(async () => {
      const res = await api.put<{ repo: string }>("/api/github/repo", { full_name: fullName });
      setGh({ connected: true, user, repo: res.repo });
      await refresh();
    });
  const changeRepo = () =>
    run(async () => {
      await api.delete("/api/github/repo");
      setGh({ connected: true, user, repo: null });
      await refresh();
    });
  const makeKey = () => run(async () => setNewKey((await api.post<{ key: string }>("/api/backup/key")).key));
  const confirmKey = () =>
    run(async () => {
      const res = await api.post<{ ok?: boolean }>("/api/backup/key/confirm", { last4: last4.trim() });
      if (res?.ok === false) throw new Error(t("backup.key.last4.wrong"));     // the key shown stays on screen
      setNewKey(null);
      setLast4("");
      await refresh();                                      // the engine clears an old "save your key" error by itself
      setState((prev) => ({ ...(prev ?? current), key_ready: true }));
    });
  const backupNow = () =>
    run(async () => {
      const res = await api.post<{ status: BackupState["status"] }>("/api/backup/run");
      setState({ ...current, status: res.status });
    });
  const openRestore = () => run(async () => setEntries(await api.get<BackupEntry[]>("/api/backup/list")));
  const restore = (force = false) => {
    setAskBusy(false);
    return run(async () => {
      try {
        const res = await api.post<{ reconnect: Reconnect[] }>(force ? "/api/backup/restore?force=true" : "/api/backup/restore",
          { name: picked, key: restoreKey.trim() });
        setRestoreKey("");
        announceRestore();                                  // the restart screen says why Hokma restarts
        setReconnect(res.reconnect);
      } catch (cause) {
        if (cause instanceof ApiError && cause.code === "restore.in_use") setAskBusy(true); // an agent is working: ask first
        else throw cause;
      }
    });
  };
  const saveKeyFile = () => {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([`${newKey}\n`], { type: "text/plain" }));
    link.download = "hokma-recovery-key.txt";
    link.click();
    URL.revokeObjectURL(link.href);
  };
  const when = (iso: string) => formatWhen(iso, locale);        // the engine's times are UTC without a zone
  const dismissRestoreFailure = () =>
    run(async () => {
      await api.delete("/api/backup/restore-failed");
      await refresh();
    });

  return (
    <div style={stack}>
      <Pane style={{ ...stack, padding: 16 }}>
        <span className="h2">{t("backup.step1")}</span>
        {connected && picking ? (
          <>
            <span style={inline}>
              <Pill tone="ok" data-testid="github-status">{t("backup.github.connected", { user: user ?? "" })}</Pill>
              <Button disabled={busy} onClick={() => void disconnect()}>{t("backup.github.disconnect")}</Button>
            </span>
            <p className="p">{t("backup.github.pick")}</p>
            {repos !== null && repos.length > 0 ? (
              <span style={stack} role="radiogroup" aria-label={t("backup.github.pick")} data-testid="repo-list">
                {repos.map((r) => (
                  <Button key={r.full_name} role="radio" aria-checked={false} disabled={busy} onClick={() => void pickRepo(r.full_name)}>
                    {r.full_name}
                  </Button>
                ))}
              </span>
            ) : repos !== null ? (
              <p className="p" data-testid="repo-empty">{t("backup.github.pick.empty")}</p>
            ) : null}
            <span style={inline}>
              {links?.install_url ? <a className="btn primary" href={links.install_url} target="_blank" rel="noreferrer">{t("backup.github.pick.install")}</a> : null}
              {links?.new_repo_url ? <a className="btn" href={links.new_repo_url} target="_blank" rel="noreferrer">{t("backup.github.pick.new")}</a> : null}
              <Button disabled={busy} onClick={() => void loadRepos().catch((cause) => setError(cause instanceof Error ? cause.message : t("api.error")))}>
                {t("backup.github.pick.refresh")}
              </Button>
            </span>
          </>
        ) : connected ? (
          <>
            <span style={inline}>
              <Pill tone="ok" data-testid="github-status">{t("backup.github.connected", { user: user ?? "" })}</Pill>
              <Button disabled={busy} onClick={() => void disconnect()}>{t("backup.github.disconnect")}</Button>
            </span>
            <span style={inline}>
              <Pill tone="ok" data-testid="github-repo">{t("backup.github.repo", { repo: repo ?? "" })}</Pill>
              <Button disabled={busy} onClick={() => void changeRepo()}>{t("backup.github.change")}</Button>
            </span>
          </>
        ) : offline ? (
          <span style={inline}>
            <Pill tone="warn" data-testid="github-offline">{t("backup.github.offline")}</Pill>
            <Button onClick={() => void github.reload()}>{t("backup.github.retry")}</Button>
          </span>
        ) : device ? (
          <>
            <p className="p">{t("backup.github.code")}</p>
            <span className="h2" data-testid="device-code" style={{ letterSpacing: 2 }}>{device.user_code}</span>
            <span style={inline}>
              <a className="btn primary" href={device.verification_uri} target="_blank" rel="noreferrer">{t("backup.github.open")}</a>
              <Pill tone="warn">{t("backup.github.waiting")}</Pill>
            </span>
          </>
        ) : (
          <>
            {(gh ?? github.data)?.legacy ? (
              <span style={stack} role="alert" data-testid="github-legacy">
                <p className="p">{t("backup.github.legacy")}</p>
                {links?.legacy_url ? <a className="btn" href={links.legacy_url} target="_blank" rel="noreferrer">{t("backup.github.legacy.revoke")}</a> : null}
              </span>
            ) : null}
            <p className="p">{t("backup.github.hint")}</p>
            <p className="p" style={{ fontSize: 12 }}>{t("backup.github.scope")}</p>
            <div><Button variant="primary" disabled={busy} onClick={() => void startConnect()}>{t("backup.github.connect")}</Button></div>
          </>
        )}
      </Pane>

      <Pane style={{ ...stack, padding: 16 }}>
        <span className="h2">{t("backup.step2")}</span>
        {newKey ? (
          <>
            <p className="p">{t("backup.key.once")}</p>
            <span className="h2" data-testid="recovery-key" style={{ letterSpacing: 1, wordBreak: "break-all" }}>{newKey}</span>
            <span style={inline}>
              <Button onClick={() => void navigator.clipboard?.writeText(newKey)}>{t("backup.key.copy")}</Button>
              <Button onClick={saveKeyFile}>{t("backup.key.save")}</Button>
            </span>
            <Field label={t("backup.key.last4")} hint={t("backup.key.last4.hint")}>
              {(control) => <Input {...control} autoComplete="off" value={last4} maxLength={4} onChange={(e) => setLast4(e.target.value)} />}
            </Field>
            <div><Button variant="primary" disabled={busy || last4.trim().length !== 4} onClick={() => void confirmKey()}>{t("backup.key.confirm")}</Button></div>
          </>
        ) : current.key_ready ? (
          <Pill tone="ok" data-testid="key-status">{t("backup.key.ready")}</Pill>
        ) : (
          <>
            <p className="p">{t("backup.key.hint")}</p>
            <div><Button variant="primary" disabled={busy} onClick={() => void makeKey()}>{t("backup.key.create")}</Button></div>
          </>
        )}
      </Pane>

      <Pane style={{ ...stack, padding: 16 }}>
        <span className="h2">{t("backup.step3")}</span>
        <span style={inline}>
          <Pill tone={TONE[current.status]} data-testid="backup-status">{t(`backup.status.${current.status}`)}</Pill>
          {current.message ? <span className="p" data-testid="backup-message">{current.message}</span> : null}
        </span>
        <span className="p">
          {current.last_backup_at
            ? t("backup.last", { when: when(current.last_backup_at), size: sizeLabel(current.last_size ?? 0) })
            : t("backup.never")}
        </span>
        <span className="p">{t("backup.estimate", { size: sizeLabel(current.total_size_estimate) })}</span>
        {current.omitted.length > 0 ? (
          <>
            <span className="p">{t("backup.omitted")}</span>
            <ul className="p" data-testid="omitted" style={{ margin: 0, paddingLeft: 18 }}>
              {current.omitted.map((o) => (
                <li key={o.path}>{OMITTED_REASONS.includes(o.reason) ? `${o.path}: ${t(`backup.omitted.${o.reason}`)}` : o.path}</li>
              ))}
            </ul>
          </>
        ) : null}
        <div>
          <Button variant="primary" disabled={busy || !connected || repo === null || !current.key_ready || current.status === "running"} onClick={() => void backupNow()}>
            {t("backup.now")}
          </Button>
        </div>
        <p className="p" style={{ fontSize: 12 }}>{t("backup.responsibility")}</p>
        {current.restore_failed ? (
          <span style={inline} role="alert" data-testid="restore-failed">
            <Pill tone="bad">{t("backup.restore.failed", { when: when(current.restore_failed.at) })}</Pill>
            <Button disabled={busy} onClick={() => void dismissRestoreFailure()}>{t("backup.restore.failed.dismiss")}</Button>
          </span>
        ) : null}
      </Pane>

      <Pane style={{ ...stack, padding: 16 }}>
        <span className="h2">{t("backup.restore")}</span>
        {reconnect ? (
          <>
            <p className="p" data-testid="restore-done">{t("backup.restore.done")}</p>
            {reconnect.length > 0 ? (
              <>
                <span className="p">{t("backup.restore.reconnect")}</span>
                <ul className="p" style={{ margin: 0, paddingLeft: 18 }}>
                  {reconnect.map((r) => <li key={`${r.kind}:${r.name}`}>{t(`backup.restore.reconnect.${r.kind}`, { name: r.name })}</li>)}
                </ul>
              </>
            ) : null}
          </>
        ) : entries === null ? (
          <>
            <p className="p">{t("backup.restore.hint")}</p>
            <div><Button disabled={busy || !connected || repo === null} onClick={() => void openRestore()}>{t("backup.restore")}</Button></div>
          </>
        ) : entries.length === 0 ? (
          <p className="p">{t("backup.restore.empty")}</p>
        ) : (
          <>
            <span style={stack} role="radiogroup" aria-label={t("backup.restore.pick")}>
              {entries.map((e) => (
                <Button key={e.name} role="radio" aria-checked={picked === e.name} variant={picked === e.name ? "primary" : "default"} onClick={() => setPicked(e.name)}>
                  {when(e.created_at)} · {sizeLabel(e.size)}
                </Button>
              ))}
            </span>
            <Field label={t("backup.restore.key")}>
              {(control) => <Input {...control} type="password" autoComplete="off" value={restoreKey} onChange={(e) => setRestoreKey(e.target.value)} />}
            </Field>
            <p className="p" style={{ fontSize: 12 }}>{t("backup.restore.warn")}</p>
            <div><Button variant="primary" disabled={busy || picked === "" || restoreKey.trim() === ""} onClick={() => void restore()}>{t("backup.restore.confirm")}</Button></div>
            {askBusy ? <BusyConfirmDialog title={t("backup.restore.confirm")} onContinue={() => void restore(true)} onCancel={() => setAskBusy(false)} /> : null}
          </>
        )}
      </Pane>
      {error !== null ? <p className="p" role="alert" style={{ color: "var(--bad)" }}>{error}</p> : null}
    </div>
  );
}
