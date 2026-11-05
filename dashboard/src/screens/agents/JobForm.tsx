import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import { ApiError } from "@/api";
import { Button, Field, Input, Modal, Segmented, Select, Textarea } from "@/design";
import { useI18n } from "@/i18n";
import { jobsApi } from "./api";
import { DAYS, localTimeZone, parseWeekly, timeZones } from "./format";
import type { Day } from "./format";
import type { AgentItem, JobInput, JobItem, JobKind } from "./types";

interface Props {
  job: JobItem | null;
  agents: AgentItem[];
  onClose: () => void;
  onSaved: (saved: JobItem, wasActive: boolean) => void;
}

type Unit = "m" | "h" | "d";

const KINDS: JobKind[] = ["once", "daily", "weekly", "interval"];
const WEEKDAYS: Day[] = ["mon", "tue", "wed", "thu", "fri"];

function inOneHour(): string {
  const date = new Date(Date.now() + 60 * 60 * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** Fields of each kind, read back from the expression the backend stores. */
function initial(job: JobItem | null) {
  const base = { once: inOneHour(), time: "07:30", days: WEEKDAYS as Day[], every: "30", unit: "m" as Unit };
  if (!job) return base;
  if (job.kind === "once") return { ...base, once: job.expr };
  if (job.kind === "daily") return { ...base, time: job.expr };
  if (job.kind === "weekly") {
    const weekly = parseWeekly(job.expr);
    return { ...base, time: weekly.time, days: weekly.days };
  }
  return { ...base, every: job.expr.slice(0, -1), unit: job.expr.slice(-1) as Unit };
}

/** Create or edit a scheduled task: name, agent, instruction, frequency and time zone. Saved tasks start off. */
export function JobForm({ job, agents, onClose, onSaved }: Props) {
  const { t } = useI18n();
  const start = useMemo(() => initial(job), [job]);
  const [name, setName] = useState(job?.name ?? "");
  const [agent, setAgent] = useState(job?.agent ?? agents.find((a) => a.is_orchestrator)?.slug ?? agents[0]?.slug ?? "");
  const [prompt, setPrompt] = useState(job?.prompt ?? "");
  const [kind, setKind] = useState<JobKind>(job?.kind ?? "daily");
  const [once, setOnce] = useState(start.once);
  const [time, setTime] = useState(start.time);
  const [days, setDays] = useState<Day[]>(start.days);
  const [every, setEvery] = useState(start.every);
  const [unit, setUnit] = useState<Unit>(start.unit);
  const [zone, setZone] = useState(job?.timezone ?? localTimeZone());
  const [missing, setMissing] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const zones = useMemo(timeZones, []);

  const expression = (): string => {
    if (kind === "once") return once;
    if (kind === "daily") return time;
    if (kind === "weekly") return `${DAYS.filter((d) => days.includes(d)).join(",")} ${time}`;
    return `${every.trim()}${unit}`;
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const gaps = { name: !name.trim(), prompt: !prompt.trim(), agent: !agent, days: kind === "weekly" && days.length === 0 };
    setMissing(gaps);
    if (Object.values(gaps).some(Boolean)) return;
    setBusy(true);
    setError(null);
    const body: JobInput = { name, agent, prompt, kind, expr: expression(), timezone: zone.trim() };
    try {
      const saved = job ? await jobsApi.update(job.id, body) : await jobsApi.create(body);
      onSaved(saved, job?.active ?? false);
    } catch (cause) {
      setError(cause instanceof ApiError || cause instanceof Error ? cause.message : t("api.error"));
      setBusy(false);
    }
  };

  const required = t("tasks.form.required");
  const title = t(job ? "tasks.form.edit" : "tasks.form.create");
  return (
    <Modal label={title} onClose={onClose} style={{ width: "min(580px, 94vw)", maxHeight: "92vh", overflow: "auto" }}>
      <form onSubmit={(event) => void submit(event)} style={{ display: "flex", flexDirection: "column", gap: 14, padding: 22 }} noValidate>
        <h2 className="h1">{title}</h2>
        <Field label={t("tasks.form.name")} error={missing.name ? required : undefined}>
          {(control) => <Input {...control} value={name} maxLength={80} autoFocus onChange={(e) => setName(e.target.value)} />}
        </Field>
        <Field label={t("tasks.form.agent")} error={missing.agent ? required : undefined}>
          {(control) => (
            <Select {...control} value={agent} onChange={(e) => setAgent(e.target.value)}>
              {agents.map((a) => (
                <option key={a.slug} value={a.slug}>{a.name}</option>
              ))}
            </Select>
          )}
        </Field>
        <Field label={t("tasks.form.prompt")} hint={t("tasks.form.prompt.hint")} error={missing.prompt ? required : undefined}>
          {(control) => <Textarea {...control} rows={4} value={prompt} maxLength={4000} onChange={(e) => setPrompt(e.target.value)} />}
        </Field>

        <div className="fld">
          <span className="lbl">{t("tasks.form.kind")}</span>
          <Segmented
            label={t("tasks.form.kind")}
            value={kind}
            onChange={setKind}
            options={KINDS.map((k) => ({ value: k, label: t(`tasks.form.kind.${k}`) }))}
          />
        </div>

        {kind === "once" ? (
          <Field label={t("tasks.form.date")}>
            {(control) => <Input {...control} type="datetime-local" value={once} onChange={(e) => setOnce(e.target.value)} />}
          </Field>
        ) : null}
        {kind === "daily" || kind === "weekly" ? (
          <Field label={t("tasks.form.time")}>
            {(control) => <Input {...control} type="time" value={time} onChange={(e) => setTime(e.target.value)} />}
          </Field>
        ) : null}
        {kind === "weekly" ? (
          <div className="fld">
            <span className="lbl">{t("tasks.form.days")}</span>
            <div role="group" aria-label={t("tasks.form.days")} style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {DAYS.map((d) => (
                <Button
                  key={d}
                  size="sm"
                  variant={days.includes(d) ? "primary" : "default"}
                  aria-pressed={days.includes(d)}
                  aria-label={t(`tasks.day.${d}`)}
                  title={t(`tasks.day.${d}`)}
                  onClick={() => setDays((current) => (current.includes(d) ? current.filter((x) => x !== d) : [...current, d]))}
                >
                  {t(`tasks.day.short.${d}`)}
                </Button>
              ))}
            </div>
            {missing.days ? <span className="err" role="alert">{required}</span> : null}
          </div>
        ) : null}
        {kind === "interval" ? (
          <div className="fld">
            <span className="lbl">{t("tasks.form.every")}</span>
            <div style={{ display: "flex", gap: 8 }}>
              <Input aria-label={t("tasks.form.every")} inputMode="numeric" style={{ maxWidth: 120 }} value={every} onChange={(e) => setEvery(e.target.value)} />
              <Select aria-label={t("tasks.form.every")} value={unit} onChange={(e) => setUnit(e.target.value as Unit)}>
                {(["m", "h", "d"] as Unit[]).map((u) => (
                  <option key={u} value={u}>{t(`tasks.form.unit.${u}`)}</option>
                ))}
              </Select>
            </div>
            <span className="hint">{t("tasks.form.interval.hint")}</span>
          </div>
        ) : null}

        <Field label={t("tasks.form.timezone")}>
          {(control) => (
            <>
              <Input {...control} list="hokma-time-zones" value={zone} onChange={(e) => setZone(e.target.value)} />
              <datalist id="hokma-time-zones">
                {zones.map((z) => (
                  <option key={z} value={z} />
                ))}
              </datalist>
            </>
          )}
        </Field>

        {error ? <p className="err" role="alert">{error}</p> : null}
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <Button variant="ghost" onClick={onClose}>{t("tasks.form.cancel")}</Button>
          <Button variant="primary" type="submit" disabled={busy}>
            {t(job ? "tasks.form.submit.save" : "tasks.form.submit.create")}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
