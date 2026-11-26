import { useCallback, useEffect, useRef, useState } from "react";
import { Button, ErrorState, Icon, Input, Loading, Pill, Select } from "@/design";
import { useI18n } from "@/i18n";
import { setChatStart } from "@/screens/chat/start";
import { newDraftId } from "@/shell/tabsStorage";
import { useShell } from "@/shell/useShell";
import { failText } from "../configApi";
import { rulesApi } from "../rulesApi";
import type { AccountState, CatalogRow, ControlId, ModeAnswers, OnTrip, Proposal, Rule, RulesView, RuleScope, Suggestion } from "../rulesApi";
import type { TabProps } from "./types";

const FIXED = ["contract", "monitor", "premium_selling", "broker_rules"] as const;
type Translate = ReturnType<typeof useI18n>["t"];

/** The catalog row of an events rule is "events"; its label carries the word of the event. */
function controlLabel(t: Translate, rule: { control_id?: ControlId; match?: string | null }): string {
  if (rule.control_id === "events") return t("desks.rules.c.events", { match: rule.match ?? "" });
  return t(`desks.rules.c.${rule.control_id}`);
}

const unitText = (t: Translate, unit: string | undefined) => (unit && unit !== "assets" ? t(`desks.rules.u.${unit}`) : "");

/** The controls whose limit closes the day of an account: the owner also says what happens to the open positions. */
const TRIP_CONTROLS: ControlId[] = ["daily_loss", "daily_loss_pct", "drawdown", "drawdown_pct"];
const TRIPS: OnTrip[] = ["block_entries", "block_and_flatten", "block_and_flatten_all"];

/** The answers of the owner to "restriction or notice?" while he writes a rule. Nothing is chosen for him: an empty mode is not saved. */
interface ModeDraft { mode: "" | "restriction" | "notice"; trip: "" | OnTrip; wait: string }
const EMPTY_MODE: ModeDraft = { mode: "", trip: "", wait: "30" };

function answersOf(control: ControlId | undefined, d: ModeDraft): ModeAnswers | null {
  if (d.mode === "restriction") {
    if (control && TRIP_CONTROLS.includes(control)) return d.trip === "" ? null : { mode: "restriction", on_trip: d.trip };
    return { mode: "restriction" };
  }
  if (d.mode === "notice") {
    const wait = Number(d.wait);
    return Number.isInteger(wait) && wait >= 1 && wait <= 1440 ? { mode: "notice", timeout_min: wait } : null;
  }
  return null;
}

const draftOf = (rule: { mode?: "restriction" | "notice"; on_trip?: OnTrip; timeout_min?: number }): ModeDraft => ({
  mode: rule.mode ?? "restriction", trip: rule.on_trip ?? "", wait: String(rule.timeout_min ?? 30),
});

function modeText(t: Translate, rule: { mode?: "restriction" | "notice"; on_trip?: OnTrip; timeout_min?: number }): string {
  if (rule.mode === "notice") return t("desks.rules.mode.noticeText", { min: String(rule.timeout_min ?? 30) });
  return rule.on_trip ? t("desks.rules.mode.restrictionTrip", { trip: t(`desks.rules.trip.${rule.on_trip}`) }) : t("desks.rules.mode.restrictionText");
}

/** The two questions of every rule: restriction or notice, and what happens at the limit (loss rules) or how long a notice waits. */
function ModeFields({ control, draft, onChange }: { control: ControlId | undefined; draft: ModeDraft; onChange: (next: ModeDraft) => void }) {
  const { t } = useI18n();
  return (
    <>
      <Select aria-label={t("desks.rules.mode.ask")} value={draft.mode} onChange={(e) => onChange({ ...draft, mode: e.target.value as ModeDraft["mode"] })}>
        <option value="">{t("desks.rules.mode.ask")}</option>
        <option value="restriction">{t("desks.rules.mode.restriction")}</option>
        <option value="notice">{t("desks.rules.mode.notice")}</option>
      </Select>
      {draft.mode === "restriction" && control && TRIP_CONTROLS.includes(control) ? (
        <Select aria-label={t("desks.rules.trip.ask")} value={draft.trip} onChange={(e) => onChange({ ...draft, trip: e.target.value as ModeDraft["trip"] })}>
          <option value="">{t("desks.rules.trip.ask")}</option>
          {TRIPS.map((trip) => <option key={trip} value={trip}>{t(`desks.rules.trip.${trip}`)}</option>)}
        </Select>
      ) : null}
      {draft.mode === "notice" ? (
        <Input aria-label={t("desks.rules.mode.wait")} placeholder={t("desks.rules.mode.wait")} inputMode="numeric" maxLength={4} autoComplete="off" value={draft.wait}
          onChange={(e) => onChange({ ...draft, wait: e.target.value })} />
      ) : null}
    </>
  );
}

/** A server that answers with something else than the view is an error to show, never a crash. */
const isView = (value: unknown): value is RulesView => {
  const v = value as Partial<RulesView> | null;
  return !!v && Array.isArray(v.active) && Array.isArray(v.proposed) && Array.isArray(v.suggested) && Array.isArray(v.catalog) && Array.isArray(v.assets);
};

/** Rules of a desk (SPEC-025): controls the code applies before every order, and instructions the chief reads. */
export function RulesTab({ desk }: TabProps) {
  const { t } = useI18n();
  const shell = useShell();
  const [view, setView] = useState<RulesView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  /** The proposal that loosens a control and waits for the second, explicit confirmation of the owner. */
  const [confirming, setConfirming] = useState<string | null>(null);
  const ticket = useRef(0);

  const load = useCallback(() => {
    const mine = (ticket.current += 1);
    rulesApi.get(desk.id)
      .then((next) => {
        if (mine !== ticket.current) return;
        if (!isView(next)) throw new Error("bad rules view");
        setError(null);
        setView(next);
      })
      .catch((cause: unknown) => { if (mine === ticket.current) setError(failText(cause, t("desks.rules.error.load"))); });
  }, [desk.id, t]);

  useEffect(() => {
    setView(null);
    setError(null);
    setProblem(null);
    load();
  }, [load]);

  /** Every change answers with the whole view, so the screen never shows a state the server does not have. */
  const act = async (call: () => Promise<RulesView>): Promise<boolean> => {
    if (busy) return false;
    setBusy(true);
    setProblem(null);
    try {
      ticket.current += 1;
      const next = await call();
      if (!isView(next)) throw new Error("bad rules view");
      setView(next);
      return true;
    } catch (cause) {
      setProblem(failText(cause, t("desks.rules.error.action")));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const fix = (proposal: Proposal) => {
    const draft = newDraftId();
    setChatStart(draft, t("desks.rules.fix.start", { desk: desk.name, text: proposal.text }));
    shell.openScreen("chat", { draft });
    shell.toast({ tone: "info", text: t("desks.rules.fix.toast") });
  };

  return (
    <section className="rules-tab" aria-label={t("desks.tab.rules")}>
      <header className="rules-hd">
        <h2 className="rules-title">{t("desks.rules.title")}</h2>
        <p className="p">{t("desks.rules.text")}</p>
      </header>
      {error ? <ErrorState title={error} /> : null}
      {!error && !view ? <Loading label={t("desks.rules.loading")} /> : null}
      {view ? (
        <>
          {problem ? <p className="err" role="alert">{problem}</p> : null}
          {view.proposed.length > 0 ? (
            <section className="pane rules-block" aria-labelledby="rules-proposed">
              <span className="lbl" id="rules-proposed">{t("desks.rules.proposed.title")}</span>
              <ul className="rules-list">
                {view.proposed.map((p) => (
                  <li key={p.id} className="rules-row">
                    <Pill tone={p.kind === "control" ? "ok" : "warn"}>{t(`desks.rules.pill.${p.kind}`)}</Pill>
                    {p.kind === "control" ? (
                      <span className="rules-text">
                        {controlLabel(t, p)}
                        <b className="mono rules-value"> {String(p.value)} {unitText(t, p.unit ?? undefined)}</b>
                        {p.mode ? <span className="today-meta"> {modeText(t, p)}</span> : null}
                        {p.account_id != null ? <span className="today-meta"> {t(p.only_desk ? "desks.rules.scope.desk_account" : "desks.rules.scope.account")}</span> : null}
                        {p.loosens ? (
                          <span className="err" role="alert">
                            {" "}{t("desks.rules.proposed.loosens", { from: p.current == null ? "-" : String(p.current), to: String(p.value), unit: unitText(t, p.unit ?? undefined) })}
                          </span>
                        ) : null}
                      </span>
                    ) : <span className="rules-text">{p.text}</span>}
                    <span className="desk-hdr-actions">
                      <Button variant="ghost" size="sm" disabled={busy} onClick={() => fix(p)}>{t("desks.rules.proposed.fix")}</Button>
                      <Button variant="ghost" size="sm" disabled={busy} onClick={() => { setConfirming(null); void act(() => rulesApi.reject(desk.id, p.id)); }}>{t("desks.rules.proposed.discard")}</Button>
                      {p.loosens && confirming !== p.id ? (
                        <Button variant="primary" size="sm" disabled={busy} onClick={() => setConfirming(p.id)}>{t("desks.rules.proposed.approve")}</Button>
                      ) : p.loosens ? (
                        <>
                          <Button variant="ghost" size="sm" disabled={busy} onClick={() => setConfirming(null)}>{t("desks.rules.cancel")}</Button>
                          <Button variant="primary" size="sm" disabled={busy} onClick={() => { setConfirming(null); void act(() => rulesApi.approve(desk.id, p.id, true)); }}>{t("desks.rules.proposed.confirmLoosen")}</Button>
                        </>
                      ) : (
                        <Button variant="primary" size="sm" disabled={busy} onClick={() => void act(() => rulesApi.approve(desk.id, p.id))}>{t("desks.rules.proposed.approve")}</Button>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          <section className="pane rules-block" aria-labelledby="rules-active">
            <span className="lbl" id="rules-active">{t("desks.rules.active.title")}</span>
            {view.legacy ? <p className="hint">{t("desks.rules.legacy")}</p> : null}
            {view.active.length === 0 ? <p className="p">{t("desks.rules.empty")}</p> : (
              <ul className="rules-list">
                {view.active.map((rule) => (
                  <ActiveRow key={rule.id} rule={rule} assets={view.assets} busy={busy}
                    onValue={(value, answers) => act(() => rulesApi.editControl(desk.id, rule.id, value, answers))}
                    onMode={(answers) => act(() => rulesApi.editMode(desk.id, rule.id, answers))}
                    onText={(text) => act(() => rulesApi.editText(desk.id, rule.id, text))}
                    onRemove={() => void act(() => rulesApi.remove(desk.id, rule.id))} />
                ))}
              </ul>
            )}
          </section>

          {(view.accounts ?? []).some((a) => a.state === "day_closed") || (view.account_rules ?? []).length > 0 ? (
            <section className="pane rules-block" aria-labelledby="rules-accounts">
              <span className="lbl" id="rules-accounts">{t("desks.rules.accounts.title")}</span>
              <ul className="rules-list">
                {(view.accounts ?? []).filter((a) => a.state === "day_closed").map((a) => (
                  <ClosedRow key={a.account_id} account={a} busy={busy}
                    onLift={() => act(async () => { await rulesApi.liftDayClose(a.account_id, true); return rulesApi.get(desk.id); })} />
                ))}
                {(view.account_rules ?? []).map((rule) => (
                  <ActiveRow key={rule.id} rule={rule} assets={view.assets} busy={busy}
                    accountName={(view.accounts ?? []).find((a) => a.account_id === rule.account_id)?.name}
                    onValue={(value, answers) => act(() => rulesApi.editControl(desk.id, rule.id, value, answers))}
                    onMode={(answers) => act(() => rulesApi.editMode(desk.id, rule.id, answers))}
                    onText={async () => false}
                    onRemove={() => void act(() => rulesApi.remove(desk.id, rule.id))} />
                ))}
              </ul>
            </section>
          ) : null}

          <section className="pane rules-block rules-fixed" aria-labelledby="rules-fixed">
            <span className="lbl" id="rules-fixed"><Icon name="shield" size={12} /> {t("desks.rules.fixed.title")}</span>
            <ul className="rules-fixed-list">
              {FIXED.map((name) => <li key={name}>{t(`desks.rules.fixed.${name}`)}</li>)}
            </ul>
          </section>

          {view.suggested.length > 0 ? (
            <section className="pane rules-block" aria-labelledby="rules-suggested">
              <span className="lbl" id="rules-suggested">{t("desks.rules.suggested.title")}</span>
              <ul className="rules-list">
                {view.suggested.map((s) => (
                  <SuggestedRow key={s.id} s={s} busy={busy}
                    onAdd={(answers) => void act(() => rulesApi.add(desk.id, s.kind === "instruction"
                      ? { kind: "instruction", text: t(`desks.rules.i.${s.id}`), source: "suggested", suggested_id: s.id }
                      : { kind: "control", control_id: s.control_id, value: s.value, match: s.match ?? undefined, source: "suggested", ...answers }))}
                    onDismiss={() => void act(() => rulesApi.dismiss(desk.id, s.id))} />
                ))}
              </ul>
            </section>
          ) : null}

          <AddRule catalog={view.catalog} accounts={view.accounts ?? []} busy={busy}
            onControl={(control_id, value, match, answers, scope, accountId) => act(() => rulesApi.add(desk.id, { kind: "control", control_id, value, match, source: "hand", ...answers, scope, account_id: accountId }))}
            onInstruction={(text) => act(() => rulesApi.add(desk.id, { kind: "instruction", text, source: "hand" }))} />
        </>
      ) : null}
    </section>
  );
}

function ActiveRow({ rule, assets, busy, accountName, onValue, onMode, onText, onRemove }: {
  rule: Rule; assets: string[]; busy: boolean; accountName?: string;
  onValue: (value: number, answers: ModeAnswers) => Promise<boolean>; onMode: (answers: ModeAnswers) => Promise<boolean>;
  onText: (text: string) => Promise<boolean>; onRemove: () => void;
}) {
  const { t } = useI18n();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [modeDraft, setModeDraft] = useState<ModeDraft>(EMPTY_MODE);
  const [modeOnly, setModeOnly] = useState(false);
  const isControl = rule.kind === "control";
  const label = isControl
    ? (rule.control_id === "assets_only" ? (assets.length ? t("desks.rules.assets", { list: assets.join(", ") }) : t("desks.rules.assets.none")) : controlLabel(t, rule))
    : (rule.text ?? "");

  const start = () => { setDraft(isControl ? String(rule.value ?? "") : (rule.text ?? "")); setModeDraft(draftOf(rule)); setModeOnly(false); setEditing(true); };
  const startMode = () => { setModeDraft(draftOf(rule)); setModeOnly(true); setEditing(true); };
  const save = async () => {
    const answers = isControl ? answersOf(rule.control_id, modeDraft) : null;
    if (isControl && !answers) return;
    if (modeOnly && answers) { if (await onMode(answers)) setEditing(false); return; }
    const ok = isControl ? (Number.isFinite(Number(draft)) && draft.trim() !== "" && answers ? await onValue(Number(draft), answers) : false) : await onText(draft);
    if (ok) setEditing(false);
  };

  return (
    <li className="rules-row">
      <Pill tone={isControl ? "ok" : "warn"}>{t(`desks.rules.pill.${rule.kind}`)}</Pill>
      <span className="rules-text">
        {label}
        {isControl && rule.control_id !== "assets_only" && !editing ? <b className="mono rules-value"> {String(rule.value)} {unitText(t, rule.unit)}</b> : null}
        {!rule.on ? <span className="hint"> {t("desks.rules.notApplied")}</span> : null}
        {isControl ? <span className="today-meta"> {modeText(t, rule)}</span> : null}
        {accountName ? <span className="today-meta"> {t(rule.scope === "desk_account" ? "desks.rules.scope.desk_account" : "desks.rules.scope.account")}: {accountName}</span> : null}
      </span>
      <span className="today-meta">{t(`desks.rules.source.${rule.source}`)}</span>
      {rule.readonly && !editing ? (
        <span className="desk-hdr-actions">
          <span className="today-meta">{t("desks.rules.readonly")}</span>
          <Button variant="ghost" size="sm" disabled={busy} aria-label={t("desks.rules.mode.change") + ": " + label} onClick={startMode}>{t("desks.rules.mode.change")}</Button>
        </span>
      ) : editing ? (
        <span className="desk-hdr-actions">
          {modeOnly ? null : (
            <Input aria-label={label} value={draft} inputMode={isControl ? "decimal" : undefined} maxLength={isControl ? 12 : 500} autoComplete="off"
              onChange={(e) => setDraft(e.target.value)} onKeyDown={(e) => e.key === "Enter" && void save()} />
          )}
          {isControl ? <ModeFields control={rule.control_id} draft={modeDraft} onChange={setModeDraft} /> : null}
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => setEditing(false)}>{t("desks.rules.cancel")}</Button>
          <Button variant="primary" size="sm" disabled={busy} onClick={() => void save()}>{t("desks.rules.save")}</Button>
        </span>
      ) : (
        <span className="desk-hdr-actions">
          <Button variant="ghost" size="sm" disabled={busy} aria-label={t("desks.rules.edit") + ": " + label} onClick={start}>{t("desks.rules.edit")}</Button>
          <Button variant="ghost" size="sm" disabled={busy} aria-label={t("desks.rules.remove") + ": " + label} onClick={onRemove}>{t("desks.rules.remove")}</Button>
        </span>
      )}
    </li>
  );
}

function SuggestedRow({ s, busy, onAdd, onDismiss }: { s: Suggestion; busy: boolean; onAdd: (answers?: ModeAnswers) => void; onDismiss: () => void }) {
  const { t } = useI18n();
  const isControl = s.kind === "control";
  const [asking, setAsking] = useState(false);
  const [modeDraft, setModeDraft] = useState<ModeDraft>(EMPTY_MODE);
  const answers = answersOf(s.control_id, modeDraft);
  const label = isControl ? controlLabel(t, s) : t(`desks.rules.i.${s.id}`);
  return (
    <li className="rules-row">
      <Pill tone={isControl ? "ok" : "warn"}>{t(`desks.rules.pill.${s.kind}`)}</Pill>
      <span className="rules-text">
        {label}
        {isControl ? <b className="mono rules-value"> {String(s.value)} {unitText(t, s.unit)}</b> : null}
        <span className="hint"> {isControl ? t(`desks.rules.why.${s.id}`) : t("desks.rules.why.instruction")}</span>
      </span>
      <span className="desk-hdr-actions">
        <Button variant="ghost" size="sm" disabled={busy} aria-label={t("desks.rules.suggested.dismiss") + ": " + label} onClick={onDismiss}>{t("desks.rules.suggested.dismiss")}</Button>
        {isControl && asking ? <ModeFields control={s.control_id} draft={modeDraft} onChange={setModeDraft} /> : null}
        <Button variant="primary" size="sm" disabled={busy || (isControl && asking && !answers)} aria-label={t("desks.rules.suggested.add") + ": " + label}
          onClick={() => { if (!isControl) onAdd(); else if (!asking) setAsking(true); else if (answers) onAdd(answers); }}>{t("desks.rules.suggested.add")}</Button>
      </span>
    </li>
  );
}

/** An account that a loss rule closed for the day. Opening it by hand asks twice: the owner reads what closed it and confirms. */
function ClosedRow({ account, busy, onLift }: { account: AccountState; busy: boolean; onLift: () => Promise<boolean> }) {
  const { t } = useI18n();
  const [confirming, setConfirming] = useState(false);
  return (
    <li className="rules-row">
      <Pill tone="warn">{t("desks.rules.closed.pill")}</Pill>
      <span className="rules-text">
        {t("desks.rules.closed.text", { name: account.name, rule: account.rule_id ?? "", loss: account.loss ?? "?" })}
        {confirming ? <span className="err" role="alert"> {t("desks.rules.closed.confirm")}</span> : null}
      </span>
      <span className="desk-hdr-actions">
        {confirming ? <Button variant="ghost" size="sm" disabled={busy} onClick={() => setConfirming(false)}>{t("desks.rules.cancel")}</Button> : null}
        <Button variant="primary" size="sm" disabled={busy} onClick={() => { if (!confirming) setConfirming(true); else void onLift().then((ok) => ok && setConfirming(false)); }}>
          {confirming ? t("desks.rules.closed.lift.confirm") : t("desks.rules.closed.lift")}
        </Button>
      </span>
    </li>
  );
}

function AddRule({ catalog, accounts, busy, onControl, onInstruction }: {
  catalog: CatalogRow[]; accounts: AccountState[]; busy: boolean;
  onControl: (control: ControlId, value: number, match: string | undefined, answers: ModeAnswers, scope: RuleScope, accountId?: number) => Promise<boolean>;
  onInstruction: (text: string) => Promise<boolean>;
}) {
  const { t } = useI18n();
  const [control, setControl] = useState("");
  const [value, setValue] = useState("");
  const [match, setMatch] = useState("");
  const [text, setText] = useState("");
  const [modeDraft, setModeDraft] = useState<ModeDraft>(EMPTY_MODE);
  const [scope, setScope] = useState<RuleScope>("desk");
  const [account, setAccount] = useState("");
  const row = catalog.find((c) => c.id === control);
  const number = Number(value);
  const answers = row ? answersOf(row.id, modeDraft) : null;
  const scoped = scope !== "desk" && row?.id !== "events";
  const ready = row !== undefined && value.trim() !== "" && Number.isFinite(number) && (row.id !== "events" || match.trim() !== "") && answers !== null
    && (!scoped || account !== "");

  return (
    <section className="pane rules-block" aria-labelledby="rules-add">
      <span className="lbl" id="rules-add">{t("desks.rules.add.title")}</span>
      <div className="rules-add-row">
        <Select aria-label={t("desks.rules.add.pick")} value={control} onChange={(e) => setControl(e.target.value)}>
          <option value="">{t("desks.rules.add.choose")}</option>
          {catalog.map((c) => <option key={c.id} value={c.id}>{c.id === "events" ? t("desks.rules.c.events.pick") : t(`desks.rules.c.${c.id}`)}</option>)}
        </Select>
        {row ? <Input aria-label={t("desks.rules.add.value")} placeholder={t("desks.rules.add.value") + " (" + unitText(t, row.unit) + ")"} inputMode="decimal" maxLength={12} autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} /> : null}
        {row && accounts.length > 0 && row.id !== "events" ? (
          <Select aria-label={t("desks.rules.scope.ask")} value={scope} onChange={(e) => setScope(e.target.value as RuleScope)}>
            <option value="desk">{t("desks.rules.scope.desk")}</option>
            <option value="account">{t("desks.rules.scope.account")}</option>
            <option value="desk_account">{t("desks.rules.scope.desk_account")}</option>
          </Select>
        ) : null}
        {scoped ? (
          <Select aria-label={t("desks.rules.scope.pickAccount")} value={account} onChange={(e) => setAccount(e.target.value)}>
            <option value="">{t("desks.rules.scope.pickAccount")}</option>
            {accounts.map((a) => <option key={a.account_id} value={String(a.account_id)}>{a.name}</option>)}
          </Select>
        ) : null}
        {row ? <ModeFields control={row.id} draft={modeDraft} onChange={setModeDraft} /> : null}
        {row?.id === "events" ? <Input aria-label={t("desks.rules.add.match")} placeholder={t("desks.rules.add.match")} maxLength={40} autoComplete="off" value={match} onChange={(e) => setMatch(e.target.value)} /> : null}
        <Button variant="primary" size="sm" disabled={busy || !ready}
          onClick={async () => {
            if (row && answers && (await onControl(row.id, number, row.id === "events" ? match.trim() : undefined, answers, scoped ? scope : "desk", scoped ? Number(account) : undefined))) {
              setControl(""); setValue(""); setMatch(""); setModeDraft(EMPTY_MODE); setScope("desk"); setAccount("");
            }
          }}>
          {t("desks.rules.add.control")}
        </Button>
      </div>
      <div className="rules-add-row">
        <Input aria-label={t("desks.rules.add.text")} placeholder={t("desks.rules.add.text")} maxLength={500} autoComplete="off" value={text} onChange={(e) => setText(e.target.value)} />
        <Button variant="primary" size="sm" disabled={busy || text.trim() === ""}
          onClick={async () => { if (await onInstruction(text.trim())) setText(""); }}>
          {t("desks.rules.add.instruction")}
        </Button>
      </div>
    </section>
  );
}
