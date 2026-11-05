import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/api";
import { Button, Dot, Input, Isotype, Loading, Modal, Option, Pane, Segmented, Steps } from "@/design";
import { useI18n } from "@/i18n";
import type { Locale } from "@/i18n";
import { useShell, useShellEvent } from "@/shell/useShell";
import { setupApi } from "./api";
import { useDatabaseSwitch } from "./useDatabaseSwitch";
import { ClaudeStep } from "./ClaudeStep";
import { DatabaseChoice } from "./DatabaseChoice";
import { FolderBrowser } from "./FolderBrowser";
import type { DbChoice, SetupState, WorkdirOption } from "./types";

type StepId = "claude" | "folder" | "database" | "name" | "done";
const ALL_STEPS: StepId[] = ["claude", "folder", "database", "name", "done"];
const MAX_NAME = 60;

/** Steps a returning owner (profile already there) still has to do. The name is known; the rest is checked live. */
async function missingSteps(state: SetupState): Promise<StepId[]> {
  const missing: StepId[] = [];
  const claude = await setupApi.claude().catch(() => null);
  if (claude?.state !== "connected") missing.push("claude");
  const folder = state.workdir;
  if (!(folder.exists && folder.git && folder.manual)) missing.push("folder");
  if (!state.profile?.owner_name.trim()) missing.push("name");
  return missing;
}

function message(cause: unknown): string {
  if (cause instanceof ApiError || cause instanceof Error) return cause.message;
  return String(cause);
}

export function optionTitle(option: WorkdirOption, t: (key: string, params?: Record<string, string>) => string): string {
  if (option.id === "sync") return t("firstrun.folder.sync", { provider: option.provider ?? "" });
  return t(option.id === "disk" ? "firstrun.folder.disk" : "firstrun.folder.documents");
}

interface Props {
  /** Milliseconds between sign-in checks (tests shorten it). */
  pollMs?: number;
}

/** Slot `overlay.firstRun`: opens by itself until the setup is done, and again from Settings. */
export function FirstRun({ pollMs }: Props) {
  const { t } = useI18n();
  const shell = useShell();
  const [state, setState] = useState<SetupState | null>(null);
  const [open, setOpen] = useState(false);
  const [relaunch, setRelaunch] = useState(false);
  const [order, setOrder] = useState<StepId[]>(ALL_STEPS);
  const [returning, setReturning] = useState(false);
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [workPath, setWorkPath] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const [engine, setEngine] = useState<DbChoice>("sqlite");
  const [pgUrl, setPgUrl] = useState("");
  const [pgTested, setPgTested] = useState(false);
  const [ownerName, setOwnerName] = useState("");
  const [fundName, setFundName] = useState("");
  const [preview, setPreview] = useState("");

  const skipThemePatch = useRef(false);
  const themeApplied = useRef(false);

  const fill = useCallback((next: SetupState) => {
    setState(next);
    setWorkPath(next.workdir.exists ? next.workdir.path : (next.workdir.options[0]?.path ?? next.workdir.path));
    setEngine(next.database.engine);
    setOwnerName(next.owner.owner_name);
    setFundName(next.owner.fund_name);
  }, []);

  const load = useCallback(async () => {
    try {
      const next = await setupApi.state();
      fill(next);
      if (next.profile && !themeApplied.current) {
        themeApplied.current = true;
        if (next.profile.theme !== shell.theme) {
          skipThemePatch.current = true;
          shell.setTheme(next.profile.theme);
        }
      }
      if (next.completed) return;
      if (next.profile) {
        // Someone who updated from an earlier version already has names, a folder and a database: only what is
        // really missing is asked, and when nothing is missing the setup is closed without showing anything.
        const missing = await missingSteps(next);
        if (missing.length === 0) {
          const done = await setupApi.complete();
          setState(done);
          shell.emit("setup:changed");
          return;
        }
        setOrder([...missing, "done"]);
        setReturning(true);
      }
      setOpen(true);
    } catch {
      /* no backend or no session: the shell shows its own notices */
    }
  }, [fill, shell]);

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useShellEvent("firstrun:open", () => {
    void (async () => {
      try {
        const next = await setupApi.state();
        fill(next);
        setRelaunch(true);
        setOrder(ALL_STEPS);
        setReturning(false);
        setStep(0);
        setError(null);
        setOpen(true);
      } catch (cause) {
        shell.toast({ tone: "bad", text: message(cause) });
      }
    })();
  });

  useShellEvent("theme:changed", (payload) => {
    if (skipThemePatch.current) {
      skipThemePatch.current = false;
      return;
    }
    const theme = (payload as { theme?: "dark" | "light" } | undefined)?.theme;
    if (theme) void setupApi.patchProfile({ theme }).catch(() => undefined);
  });

  useEffect(() => {
    if (!open || order[step] !== "name") return;
    const timer = setTimeout(() => {
      setupApi.preview(ownerName, fundName).then((r) => setPreview(r.text), () => setPreview(""));
    }, 250);
    return () => clearTimeout(timer);
  }, [open, order, step, ownerName, fundName]);

  const changeLocale = (locale: Locale) => {
    shell.setLocale(locale);
    void setupApi.patchProfile({ language: locale }).catch(() => undefined);
  };

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  };

  const afterDatabase = useCallback(async () => {
    fill(await setupApi.state());
    setStep(order.indexOf("database") + 1);
  }, [fill, order]);
  const dbSwitch = useDatabaseSwitch(state?.database.engine ?? "sqlite", afterDatabase, setError);

  const id = order[step];
  const next = () => {
    if (id === "folder") {
      void run(async () => {
        await setupApi.setWorkdir(workPath);
        setStep(step + 1);
      });
    } else if (id === "database") {
      void run(async () => {
        if (engine === "postgres" && !pgTested) throw new Error(t("firstrun.db.mustTest"));
        await dbSwitch.attempt(engine, pgUrl);
      });
    } else if (id === "name") {
      void run(async () => {
        const saved = await setupApi.setOwner({ owner_name: ownerName, fund_name: fundName, language: shell.locale, theme: shell.theme });
        fill(saved);
        shell.emit("setup:changed");
        setStep(step + 1);
      });
    } else if (id === "done") {
      void run(async () => {
        const done = await setupApi.complete();
        setState(done);
        setOpen(false);
        setRelaunch(false);
        shell.emit("setup:changed");
        shell.openScreen("chat");
      });
    } else {
      setStep(step + 1);
    }
  };

  const back = () => {
    setError(null);
    if (step === 0) {
      if (relaunch) {
        setOpen(false);
        setRelaunch(false);
      }
      return;
    }
    setStep(step - 1);
  };

  if (!open) return null;

  const options = state?.workdir.options ?? [];
  const nameMissing = id === "name" && !ownerName.trim();
  const closeIfRelaunch = () => {
    if (relaunch) {
      setOpen(false);
      setRelaunch(false);
    }
  };

  return (
    <>
      <Modal
        label={t("firstrun.dialog")}
        onClose={closeIfRelaunch}
        closeOnScrim={false}
        style={{ flexDirection: "column", width: "min(780px, 100%)" }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "18px 22px 0" }}>
          <Isotype size={28} />
          <span className="disp" style={{ fontSize: 20, flex: 1 }}>
            {t("firstrun.welcome")}
          </span>
          <Segmented
            label={t("firstrun.language")}
            value={shell.locale}
            onChange={changeLocale}
            options={[
              { value: "es", label: "ES" },
              { value: "en", label: "EN" },
            ]}
          />
        </div>
        <div style={{ padding: "16px 22px", borderBottom: "1px solid var(--border)" }} aria-label={t("firstrun.steps")}>
          <Steps labels={order.map((k) => t(`firstrun.step.${k}`))} current={step} />
          {returning ? (
            <p className="p" style={{ fontSize: 12, marginTop: 10 }}>
              {t("firstrun.returning")}
            </p>
          ) : null}
        </div>

        <div style={{ flex: 1, overflow: "auto", padding: 22 }}>
          {state === null ? <Loading label={t("firstrun.loading")} /> : null}

          {state !== null && id === "claude" ? <ClaudeStep pollMs={pollMs} /> : null}

          {state !== null && id === "folder" ? (
            <div className="fade" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              <h2 className="h1">{t("firstrun.folder.title")}</h2>
              <p className="p">{t("firstrun.folder.body")}</p>
              <div role="radiogroup" aria-label={t("firstrun.folder.options")} style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 10 }}>
                {options.map((o) => (
                  <Option key={o.id} selected={workPath === o.path} onSelect={() => setWorkPath(o.path)}>
                    <span className="h2">{optionTitle(o, t)}</span>
                    <span className="mono" style={{ fontSize: 11, color: "var(--t2)", wordBreak: "break-all" }}>
                      {o.path}
                    </span>
                  </Option>
                ))}
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <Input className="mono" style={{ fontSize: 12 }} aria-label={t("firstrun.folder.chosen")} value={workPath} onChange={(e) => setWorkPath(e.target.value)} />
                <Button size="sm" onClick={() => setBrowsing(true)}>
                  {t("firstrun.folder.browse")}
                </Button>
              </div>
              <p className="p" style={{ fontSize: 12 }}>
                {t("firstrun.folder.git")}
              </p>
            </div>
          ) : null}

          {state !== null && id === "database" ? (
            <div className="fade" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              <h2 className="h1">{t("firstrun.db.title")}</h2>
              <p className="p">{t("firstrun.db.body")}</p>
              <DatabaseChoice engine={engine} onEngine={setEngine} url={pgUrl} onUrl={setPgUrl} tested={pgTested} onTested={setPgTested} />
              <span className="pill acc" style={{ alignSelf: "flex-start" }}>
                {t("firstrun.db.change")}
              </span>
            </div>
          ) : null}

          {state !== null && id === "name" ? (
            <div className="fade" style={{ display: "flex", flexWrap: "wrap", gap: 18 }}>
              <div style={{ flex: "1 1 260px", display: "flex", flexDirection: "column", gap: 10 }}>
                <h2 className="h1">{t("firstrun.name.title")}</h2>
                <label htmlFor="hk-owner" className="lbl">
                  {t("firstrun.name.owner")}
                </label>
                <Input id="hk-owner" maxLength={MAX_NAME} value={ownerName} autoComplete="off" onChange={(e) => setOwnerName(e.target.value)} />
                <label htmlFor="hk-fund" className="lbl">
                  {t("firstrun.name.fund")}
                </label>
                <Input id="hk-fund" maxLength={MAX_NAME} value={fundName} placeholder={t("firstrun.name.fundPlaceholder")} autoComplete="off" onChange={(e) => setFundName(e.target.value)} />
                <p className="p" style={{ fontSize: 12 }}>
                  {t("firstrun.name.note")}
                </p>
                {nameMissing ? (
                  <p className="err" style={{ font: "500 12px Montserrat, sans-serif", color: "var(--bad)" }}>
                    {t("firstrun.name.required")}
                  </p>
                ) : null}
              </div>
              <pre
                className="mono"
                aria-label={t("firstrun.name.preview")}
                style={{ flex: "1 1 260px", margin: 0, padding: 14, borderRadius: 12, background: "var(--term)", border: "1px solid var(--border)", fontSize: 11, lineHeight: 1.7, whiteSpace: "pre-wrap", color: "var(--t1)", maxHeight: 300, overflow: "auto" }}
              >
                {preview}
              </pre>
            </div>
          ) : null}

          {state !== null && id === "done" ? (
            <div className="fade stagger" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 10 }}>
              <Pane style={{ padding: 14, display: "flex", flexDirection: "column", gap: 4 }}>
                <span className="lbl">{t("firstrun.done.folder")}</span>
                <span className="mono" style={{ fontSize: 11, wordBreak: "break-all" }}>
                  {state.workdir.path}
                </span>
              </Pane>
              <Pane style={{ padding: 14, display: "flex", flexDirection: "column", gap: 4 }}>
                <span className="lbl">{t("firstrun.done.database")}</span>
                <span className="h2">{state.database.engine === "postgres" ? t("firstrun.db.pgName") : t("firstrun.db.sqliteName")}</span>
              </Pane>
              <Pane style={{ padding: 14, display: "flex", flexDirection: "column", gap: 4 }}>
                <span className="lbl">{t("firstrun.done.manual")}</span>
                <span className="h2">{state.workdir.manual ? t("firstrun.done.manualReady") : t("firstrun.done.manualMissing")}</span>
              </Pane>
              <Pane style={{ padding: 14, display: "flex", flexDirection: "column", gap: 4 }}>
                <span className="lbl">{t("firstrun.done.agents")}</span>
                <span className="h2">{state.agents.total > 0 ? t("firstrun.done.agentsCount", { count: String(state.agents.total - 1) }) : t("firstrun.done.agentsNone")}</span>
              </Pane>
            </div>
          ) : null}

          {error ? (
            <p className="err" role="alert" style={{ marginTop: 12, font: "500 12px Montserrat, sans-serif", color: "var(--bad)" }}>
              {error}
            </p>
          ) : null}
        </div>

        <div style={{ display: "flex", justifyContent: "space-between", gap: 10, padding: "14px 22px", borderTop: "1px solid var(--border)" }}>
          <Button variant="ghost" disabled={busy || (step === 0 && !relaunch)} onClick={back}>
            {step === 0 && relaunch ? t("firstrun.cancel") : t("firstrun.back")}
          </Button>
          <Button variant="primary" disabled={busy || state === null || nameMissing} onClick={next}>
            {busy ? (
              <>
                <Dot live /> {id === "folder" ? t("firstrun.folder.preparing") : t("firstrun.saving")}
              </>
            ) : id === "done" ? (
              t("firstrun.open")
            ) : (
              t("firstrun.next")
            )}
          </Button>
        </div>
      </Modal>
      {dbSwitch.dialog}
      {browsing ? (
        <FolderBrowser
          start={workPath}
          onClose={() => setBrowsing(false)}
          onPick={(path) => {
            setWorkPath(path);
            setBrowsing(false);
          }}
        />
      ) : null}
    </>
  );
}
