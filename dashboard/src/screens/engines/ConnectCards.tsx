import { useEffect, useRef, useState } from "react";
import { ApiError } from "@/api";
import { Button, Input, Pill, Select } from "@/design";
import { useI18n } from "@/i18n";
import { enginesApi, errorKey, type Connection, type DeviceLogin, type EnginesListing } from "./api";
import { RegionCard } from "./RegionCard";

const KEY_PROVIDERS = ["openai", "anthropic", "google", "openrouter", "moonshot", "custom"];

function safeHttps(url: string): string | null {
  try {
    const u = new URL(url);
    return u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

/** The "Connect your AI" cards other than Claude Code: ChatGPT (Codex), Gemini, and an API key. */
export function ConnectCards({ listing, onChanged, onOther }: { listing: EnginesListing | null; onChanged: () => void; onOther?: () => void }) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [region, setRegion] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [login, setLogin] = useState<DeviceLogin | null>(null);
  const [provider, setProvider] = useState("openai");
  const [key, setKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const alive = useRef(true);
  const providers = (listing?.detect.providers ?? []).filter((p) => KEY_PROVIDERS.includes(p.id));
  const cli = listing?.detect.cli ?? {};

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    if (login?.state !== "waiting") return;
    const timer = setInterval(() => {
      void enginesApi.loginStatus().then(
        async (next) => {
          if (!alive.current) return;
          setLogin(next);
          if (next.state === "done") {
            await enginesApi.addCli({ engine: "codex", auth_kind: "subscription", label: "ChatGPT (Codex)" }).catch(() => undefined);
            setDone("ChatGPT (Codex)");
            onChanged();
          }
        },
        () => undefined,
      );
    }, 2000);
    return () => clearInterval(timer);
  }, [login?.state, onChanged]);

  const verify = async (conn: Connection, label: string) => {
    const result = await enginesApi.test(conn.id);
    if (!alive.current) return;
    if (result.ok) {
      setDone(label);
    } else if (result.code === "region") {
      setRegion(label);
    } else {
      setError(t(errorKey(result.code), { provider: label, cli: label }));
    }
    onChanged();
  };

  const guard = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    setRegion(null);
    setDone(null);
    try {
      await fn();
    } catch (cause) {
      if (alive.current) setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  const startCodex = () =>
    guard(async () => {
      const next = await enginesApi.loginStart();
      setLogin(next);
      if (next.state === "no_cli") setError(t("eng.err.not_found", { cli: "Codex" }));
    });

  const addGemini = () =>
    guard(async () => {
      const conn = await enginesApi.addCli({ engine: "gemini_cli", auth_kind: "subscription", label: "Gemini" });
      await verify(conn, "Gemini");
    });

  const addKey = () =>
    guard(async () => {
      const info = providers.find((p) => p.id === provider);
      const conn = await enginesApi.addApi({ provider, key: key.trim(), ...(baseUrl.trim() ? { base_url: baseUrl.trim() } : {}) });
      setKey("");
      await verify(conn, info?.label ?? provider);
    });

  const link = login?.url ? safeHttps(login.url) : null;
  const custom = provider === "custom";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div className="pane" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 8 }}>
        <h3 className="h2">{t("eng.codex.title")}</h3>
        <p className="p">{t("eng.codex.body")}</p>
        {cli.codex === false ? (
          <Pill tone="warn" style={{ alignSelf: "flex-start" }}>
            {t("eng.err.not_found", { cli: "Codex" })}
          </Pill>
        ) : null}
        <Button size="sm" style={{ alignSelf: "flex-start" }} disabled={busy || cli.codex === false} onClick={() => void startCodex()}>
          {t("eng.codex.login")}
        </Button>
        {login?.state === "waiting" ? (
          <div role="status" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <p className="p">{t("eng.codex.steps")}</p>
            {link ? (
              <a href={link} target="_blank" rel="noopener noreferrer" style={{ color: "var(--acc)", fontSize: 13 }}>
                {link}
              </a>
            ) : null}
            {login.code ? (
              <code className="mono" aria-label={t("eng.codex.code")} style={{ fontSize: 22, letterSpacing: 2 }}>
                {login.code}
              </code>
            ) : null}
          </div>
        ) : null}
        {login?.state === "failed" ? (
          <p className="err" role="alert" style={{ color: "var(--bad)", fontSize: 12 }}>
            {t("eng.err.auth", { provider: "ChatGPT" })}
          </p>
        ) : null}
      </div>

      <div className="pane" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 8 }}>
        <h3 className="h2">{t("eng.gemini.title")}</h3>
        <p className="p">{t("eng.gemini.body")}</p>
        {cli.gemini === false ? (
          <Pill tone="warn" style={{ alignSelf: "flex-start" }}>
            {t("eng.err.not_found", { cli: "Gemini" })}
          </Pill>
        ) : null}
        <Button size="sm" style={{ alignSelf: "flex-start" }} disabled={busy || cli.gemini === false} onClick={() => void addGemini()}>
          {t("eng.gemini.connect")}
        </Button>
      </div>

      <div className="pane" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 8 }}>
        <h3 className="h2">{t("eng.key.title")}</h3>
        <label className="lbl" htmlFor="hk-eng-provider">
          {t("eng.key.provider")}
        </label>
        <Select id="hk-eng-provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
          {providers.map((p) => (
            <option key={p.id} value={p.id}>
              {p.id === "custom" ? t("eng.key.custom") : p.label}
            </option>
          ))}
        </Select>
        {custom ? (
          <>
            <label className="lbl" htmlFor="hk-eng-url">
              {t("eng.key.baseUrl")}
            </label>
            <Input id="hk-eng-url" className="mono" autoComplete="off" spellCheck={false} placeholder="https://" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
          </>
        ) : null}
        <label className="lbl" htmlFor="hk-eng-key">
          {t("eng.key.label")}
        </label>
        <Input id="hk-eng-key" type="password" className="mono" autoComplete="off" spellCheck={false} value={key} onChange={(e) => setKey(e.target.value)} />
        <Button size="sm" variant="primary" style={{ alignSelf: "flex-start" }} disabled={busy || !key.trim() || (custom && !baseUrl.trim())} onClick={() => void addKey()}>
          {busy ? t("eng.testing") : t("eng.key.connect")}
        </Button>
      </div>

      {region ? <RegionCard provider={region} onOther={onOther ?? (() => setRegion(null))} /> : null}
      {done ? (
        <Pill tone="ok" role="status" style={{ alignSelf: "flex-start" }}>
          {t("eng.connected", { name: done })}
        </Pill>
      ) : null}
      {error ? (
        <p className="err" role="alert" style={{ font: "500 12px Montserrat, sans-serif", color: "var(--bad)" }}>
          {error}
        </p>
      ) : null}
    </div>
  );
}
