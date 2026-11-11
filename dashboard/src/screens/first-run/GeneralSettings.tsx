import { useCallback, useEffect, useState } from "react";
import { ApiError, deviceKeySupported, getBrowserPref, openInBrowser, setBrowserPref, isFirefox, logoutSession, useSessionInfo } from "@/api";
import { Button, ErrorState, Input, Loading, Segmented, Toggle } from "@/design";
import { useI18n } from "@/i18n";
import type { Locale } from "@/i18n";
import { PermissionsSettings } from "@/screens/permissions/PermissionsSettings";
import { useShell, useShellEvent } from "@/shell/useShell";
import { setupApi } from "./api";
import { DatabaseChoice } from "./DatabaseChoice";
import { FolderBrowser } from "./FolderBrowser";
import type { DbChoice, MigrationDto, SetupState } from "./types";
import { useDatabaseSwitch } from "./useDatabaseSwitch";

const MAX_NAME = 60;

function message(cause: unknown): string {
  if (cause instanceof ApiError || cause instanceof Error) return cause.message;
  return String(cause);
}

/** Settings > General: names, language, folder, database, light mode and "run the setup again". */
export function GeneralSettings() {
  const { t } = useI18n();
  const shell = useShell();
  const [state, setState] = useState<SetupState | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [ownerName, setOwnerName] = useState("");
  const [fundName, setFundName] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const [changingDb, setChangingDb] = useState(false);
  const [engine, setEngine] = useState<DbChoice>("sqlite");
  const [pgUrl, setPgUrl] = useState("");
  const [pgTested, setPgTested] = useState(false);
  const [report, setReport] = useState<MigrationDto | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const session = useSessionInfo();
  const firefox = isFirefox();
  const [always, setAlways] = useState(false);
  useEffect(() => {
    getBrowserPref().then(setAlways, () => undefined);
  }, []);
  const changeAlways = (on: boolean) =>
    run(async () => {
      await setBrowserPref(on);
      setAlways(on);
    });

  const apply = useCallback((next: SetupState) => {
    setState(next);
    setOwnerName(next.owner.owner_name);
    setFundName(next.owner.fund_name);
    setEngine(next.database.engine);
  }, []);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      apply(await setupApi.state());
    } catch (cause) {
      setLoadError(message(cause));
    }
  }, [apply]);

  useEffect(() => {
    void load();
  }, [load]);

  useShellEvent("setup:changed", () => void load());

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

  const saveNames = () =>
    run(async () => {
      const saved = await setupApi.setOwner({ owner_name: ownerName, fund_name: fundName, language: shell.locale });
      apply(saved);
      shell.emit("setup:changed");
      shell.toast({ tone: "ok", text: t("general.namesSaved") });
    });

  const changeFolder = (path: string) => {
    setBrowsing(false);
    void run(async () => {
      await setupApi.setWorkdir(path);
      apply(await setupApi.state());
      shell.emit("setup:changed");
      shell.toast({ tone: "ok", text: t("general.folderChanged") });
    });
  };

  const afterDatabase = useCallback(
    async (done: MigrationDto | null) => {
      apply(await setupApi.state());
      setChangingDb(false);
      setReport(done);
      if (done) shell.toast({ tone: "ok", text: t("general.databaseChanged") });
    },
    [apply, shell, t],
  );
  const dbSwitch = useDatabaseSwitch(state?.database.engine ?? "sqlite", afterDatabase, setError);

  const applyDatabase = () =>
    run(async () => {
      if (engine === "postgres" && !pgTested) throw new Error(t("firstrun.db.mustTest"));
      setReport(null);
      await dbSwitch.attempt(engine, pgUrl);
    });

  const changeLocale = (locale: Locale) => {
    shell.setLocale(locale);
    void setupApi.patchProfile({ language: locale }).catch(() => undefined);
  };

  const closeSession = () =>
    run(async () => {
      try {
        await logoutSession();
      } catch (cause) {
        if (cause instanceof ApiError && cause.status === 401) return;     // the session was already gone: the entry screen shows
        throw new Error(cause instanceof ApiError && cause.code !== "http_error" ? cause.message : t("session.logout.failed"));
      }
    });

  const relaunch = () => {
    shell.closeSettings();
    shell.emit("firstrun:open");
  };

  if (loadError) return <ErrorState message={loadError} onRetry={() => void load()} />;
  if (state === null) return <Loading />;

  const saved = state.profile;
  const unchanged = ownerName === state.owner.owner_name && fundName === state.owner.fund_name;
  const dbName = state.database.engine === "postgres" ? t("firstrun.db.pgName") : t("firstrun.db.sqliteName");

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, maxWidth: 560 }}>
      <label htmlFor="hk-general-owner" className="lbl">
        {t("general.owner")}
      </label>
      <Input id="hk-general-owner" maxLength={MAX_NAME} value={ownerName} autoComplete="off" onChange={(e) => setOwnerName(e.target.value)} />
      <label htmlFor="hk-general-fund" className="lbl">
        {t("general.fund")}
      </label>
      <Input id="hk-general-fund" maxLength={MAX_NAME} value={fundName} autoComplete="off" onChange={(e) => setFundName(e.target.value)} />
      <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
        <Button size="sm" variant="primary" disabled={busy || !ownerName.trim() || (unchanged && saved !== null)} onClick={() => void saveNames()}>
          {t("general.saveNames")}
        </Button>
        {saved === null ? <span className="p">{t("general.noProfile")}</span> : null}
      </div>

      <span className="lbl">{t("general.folder")}</span>
      <div style={{ display: "flex", gap: 8 }}>
        <Input className="mono" style={{ fontSize: 12 }} readOnly aria-label={t("general.folder")} value={state.workdir.path} />
        <Button size="sm" disabled={busy} onClick={() => setBrowsing(true)}>
          {t("general.folderChange")}
        </Button>
      </div>
      <span className="p" style={{ fontSize: 12 }}>
        {t("general.folderKeep")}
      </span>

      <span className="lbl">{t("general.database")}</span>
      <div style={{ display: "flex", gap: 8, alignItems: "center", justifyContent: "space-between" }}>
        <span className="p">
          {dbName}
          {state.database.location ? <span className="mono" style={{ fontSize: 11, marginLeft: 8 }}>{state.database.location}</span> : null}
        </span>
        <Button size="sm" disabled={busy} onClick={() => setChangingDb((v) => !v)}>
          {t("general.databaseChange")}
        </Button>
      </div>
      {report ? (
        <div className="pane fade" role="status" aria-label={t("general.migration.title")} style={{ padding: 14, display: "flex", flexDirection: "column", gap: 6 }}>
          <span className="lbl">{t("general.migration.title")}</span>
          <span className="p">{report.message}</span>
          <span className="p" style={{ fontSize: 12 }}>
            {t("general.migration.summary", { rows: String(report.rows), tables: String(Object.keys(report.counts).length), version: String(report.schema_version) })}
          </span>
          {report.notes.map((note) => (
            <span key={note} className="p" style={{ fontSize: 12 }}>
              {note}
            </span>
          ))}
        </div>
      ) : null}
      {changingDb ? (
        <div className="fade" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <DatabaseChoice engine={engine} onEngine={setEngine} url={pgUrl} onUrl={setPgUrl} tested={pgTested} onTested={setPgTested} />
          <div style={{ display: "flex", gap: 8 }}>
            <Button size="sm" variant="primary" disabled={busy} onClick={() => void applyDatabase()}>
              {t("general.databaseApply")}
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setChangingDb(false)}>
              {t("general.databaseCancel")}
            </Button>
          </div>
        </div>
      ) : null}

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", paddingTop: 4 }}>
        <span className="p">{t("general.language")}</span>
        <Segmented
          label={t("general.language")}
          value={shell.locale}
          onChange={changeLocale}
          options={[
            { value: "es", label: "ES" },
            { value: "en", label: "EN" },
          ]}
        />
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span className="p">{t("general.light")}</span>
        <Toggle label={t("general.light")} checked={shell.theme === "light"} onChange={(on) => shell.setTheme(on ? "light" : "dark")} />
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }} data-testid="browser-section">
        <span className="p">{t("browser.openDescription")}</span>
        <div>
          <Button size="sm" disabled={busy} onClick={() => void run(openInBrowser)} data-testid="browser-open">
            {t("browser.open")}
          </Button>
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span className="p">{t("browser.always")}</span>
          <Toggle label={t("browser.always")} checked={always} onChange={(on) => void changeAlways(on)} />
        </div>
        <span className="p">{t("browser.alwaysDescription")}</span>
      </div>

      <PermissionsSettings />

      <div className="sep" />
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }} data-testid="session-section">
        <span className="h2">{t("session.logout.title")}</span>
        <span className="p">{t("session.logout.description")}</span>
        {firefox ? (
          <span className="p" role="note" data-testid="session-firefox">
            {t("session.firefoxNotice")}
          </span>
        ) : null}
        {!firefox && (session.storageUnavailable || !deviceKeySupported()) ? (
          <span className="p" role="note" data-testid="session-no-storage">
            {t("session.noStorage")}
          </span>
        ) : null}
        <div>
          <Button size="sm" disabled={busy} onClick={() => void closeSession()}>
            {t("session.logout.button")}
          </Button>
        </div>
      </div>

      <div className="sep" />
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span className="h2">{t("general.initial")}</span>
          <span className="p">{t("general.initialBody")}</span>
        </div>
        <Button size="sm" onClick={relaunch}>
          {t("general.relaunch")}
        </Button>
      </div>

      {error ? (
        <p className="err" role="alert" style={{ font: "500 12px Montserrat, sans-serif", color: "var(--bad)" }}>
          {error}
        </p>
      ) : null}
      {dbSwitch.dialog}
      {browsing ? <FolderBrowser start={state.workdir.path} onClose={() => setBrowsing(false)} onPick={changeFolder} /> : null}
    </div>
  );
}
