import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/api";
import { Button, Dot, Input, Loading, Pill } from "@/design";
import { useI18n } from "@/i18n";
import { setupApi } from "./api";
import type { ClaudeDto } from "./types";

const INSTALL_MAC = "curl -fsSL https://claude.ai/install.sh | bash";
const INSTALL_WIN = "irm https://claude.ai/install.ps1 | iex";

function safeLink(url: string | null): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
}

export function ClaudeStep({ pollMs = 3000 }: { pollMs?: number }) {
  const { t } = useI18n();
  const [claude, setClaude] = useState<ClaudeDto | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [waiting, setWaiting] = useState(false);
  const [code, setCode] = useState("");
  const [codeSent, setCodeSent] = useState(false);
  const alive = useRef(true);

  const check = useCallback(async () => {
    try {
      const next = await setupApi.claude();
      if (!alive.current) return;
      setClaude(next);
      setError(null);
      if (next.state === "connected") setWaiting(false);
    } catch (cause) {
      if (alive.current) setError(cause instanceof ApiError ? cause.message : String(cause));
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void check();
    return () => {
      alive.current = false;
    };
  }, [check]);

  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(() => void check(), pollMs);
    return () => clearInterval(timer);
  }, [waiting, pollMs, check]);

  const login = async () => {
    setError(null);
    setCodeSent(false);
    try {
      const info = await setupApi.login();
      setWaiting(true);
      setClaude((prev) => (prev ? { ...prev, login: info } : prev));
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    }
  };

  const sendCode = async () => {
    setError(null);
    try {
      await setupApi.sendCode(code.trim());
      setCodeSent(true);
      setCode("");
      setTimeout(() => void check(), 800);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    }
  };

  const link = safeLink(claude?.login.url ?? null);

  return (
    <div className="fade" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <h2 className="h1">{t("firstrun.claude.title")}</h2>
      <p className="p">{t("firstrun.claude.body")}</p>

      {claude === null && !error ? <Loading inline label={t("firstrun.claude.checking")} /> : null}

      {claude?.state === "connected" ? (
        <Pill tone="ok" style={{ alignSelf: "flex-start" }} role="status">
          <Dot live />
          {claude.plan ? t("firstrun.claude.connectedPlan", { plan: claude.plan }) : t("firstrun.claude.connected")}
        </Pill>
      ) : null}

      {claude?.state === "no_session" ? (
        <>
          <Pill tone="warn" style={{ alignSelf: "flex-start" }} role="status">
            {t("firstrun.claude.noSession")}
          </Pill>
          <Button style={{ alignSelf: "flex-start" }} variant="primary" onClick={() => void login()}>
            {t("firstrun.claude.login")}
          </Button>
          {waiting ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <p className="p">{t("firstrun.claude.loginWaiting")}</p>
              {link ? (
                <a href={link} target="_blank" rel="noopener noreferrer" style={{ color: "var(--acc)", fontSize: 13 }}>
                  {t("firstrun.claude.loginLink")}
                </a>
              ) : null}
              <label htmlFor="hk-claude-code" className="lbl">
                {t("firstrun.claude.codeLabel")}
              </label>
              <div style={{ display: "flex", gap: 8 }}>
                <Input id="hk-claude-code" className="mono" autoComplete="off" spellCheck={false} value={code} onChange={(e) => setCode(e.target.value)} />
                <Button size="sm" disabled={!code.trim()} onClick={() => void sendCode()}>
                  {t("firstrun.claude.codeSend")}
                </Button>
              </div>
              {codeSent ? <Loading inline label={t("firstrun.claude.checking")} /> : null}
            </div>
          ) : null}
        </>
      ) : null}

      {claude?.state === "no_cli" ? (
        <>
          <Pill tone="warn" style={{ alignSelf: "flex-start" }} role="status">
            {t("firstrun.claude.noCli")}
          </Pill>
          <p className="h2">{t("firstrun.claude.installTitle")}</p>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <span className="lbl">{t("firstrun.claude.installMac")}</span>
            <code className="mono" style={{ fontSize: 12 }}>{INSTALL_MAC}</code>
            <span className="lbl">{t("firstrun.claude.installWin")}</span>
            <code className="mono" style={{ fontSize: 12 }}>{INSTALL_WIN}</code>
          </div>
        </>
      ) : null}

      {claude !== null && claude.state !== "connected" ? (
        <>
          <p className="p" style={{ fontSize: 12 }}>
            {t("firstrun.claude.skipWarn")}
          </p>
          <Button size="sm" style={{ alignSelf: "flex-start" }} onClick={() => void check()}>
            {t("firstrun.claude.recheck")}
          </Button>
        </>
      ) : null}

      {!error && claude?.state === "no_session" && claude.login.error ? (
        <p className="err" role="alert" style={{ font: "500 12px Montserrat, sans-serif", color: "var(--bad)" }}>
          {claude.login.error}
        </p>
      ) : null}

      {error ? (
        <p className="err" role="alert" style={{ font: "500 12px Montserrat, sans-serif", color: "var(--bad)" }}>
          {error}
        </p>
      ) : null}
    </div>
  );
}
