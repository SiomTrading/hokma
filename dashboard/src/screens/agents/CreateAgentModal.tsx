import { useState } from "react";
import type { FormEvent } from "react";
import { ApiError } from "@/api";
import { Button, Field, Input, Modal, Segmented, Textarea } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { agentsApi } from "./api";
import { MODELS } from "./format";
import { OpusConfirm } from "./OpusConfirm";
import type { ModelAlias } from "./types";

interface Props {
  onClose: () => void;
  onCreated: () => void;
}

/** "Create my first agent": name, description, model and instructions. The backend writes the agent's file. */
export function CreateAgentModal({ onClose, onCreated }: Props) {
  const { t } = useI18n();
  const shell = useShell();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [model, setModel] = useState<ModelAlias>("sonnet");
  const [instructions, setInstructions] = useState("");
  const [missing, setMissing] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [askOpus, setAskOpus] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const gaps = { name: !name.trim(), description: !description.trim(), instructions: !instructions.trim() };
    setMissing(gaps);
    if (gaps.name || gaps.description || gaps.instructions) return;
    setBusy(true);
    setError(null);
    try {
      const created = await agentsApi.create({ name, description, model, instructions });
      shell.toast({ tone: "ok", text: t("agents.create.done", { name: created.name, slug: created.slug }) });
      shell.emit("agents:changed");
      onCreated();
      onClose();
    } catch (cause) {
      setError(cause instanceof ApiError || cause instanceof Error ? cause.message : t("api.error"));
      setBusy(false);
    }
  };

  const required = t("agents.create.required");
  return (
    <>
    <Modal label={t("agents.create.title")} onClose={onClose} style={{ width: "min(560px, 94vw)" }}>
      <form onSubmit={(event) => void submit(event)} style={{ display: "flex", flexDirection: "column", gap: 14, padding: 22 }} noValidate>
        <h2 className="h1">{t("agents.create.title")}</h2>
        <Field label={t("agents.create.name")} hint={t("agents.create.name.hint")} error={missing.name ? required : undefined}>
          {(control) => <Input {...control} value={name} maxLength={60} autoFocus onChange={(e) => setName(e.target.value)} />}
        </Field>
        <Field label={t("agents.create.description")} hint={t("agents.create.description.hint")} error={missing.description ? required : undefined}>
          {(control) => <Textarea {...control} rows={2} value={description} maxLength={500} onChange={(e) => setDescription(e.target.value)} />}
        </Field>
        <div className="fld">
          <span className="lbl">{t("agents.create.model")}</span>
          <Segmented
            label={t("agents.create.model")}
            value={model}
            onChange={(next) => (next === "opus" ? setAskOpus(true) : setModel(next))}
            options={MODELS.map((m) => ({ value: m, label: t(`agents.model.${m}`) }))}
          />
          <span className="hint">{t(`agents.model.hint.${model}`)}</span>
        </div>
        <Field label={t("agents.create.instructions")} hint={t("agents.create.instructions.hint")} error={missing.instructions ? required : undefined}>
          {(control) => <Textarea {...control} rows={6} value={instructions} maxLength={8000} onChange={(e) => setInstructions(e.target.value)} />}
        </Field>
        {error ? <p className="err" role="alert">{error}</p> : null}
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <Button variant="ghost" onClick={onClose}>{t("agents.create.cancel")}</Button>
          <Button variant="primary" type="submit" disabled={busy}>{t("agents.create.submit")}</Button>
        </div>
      </form>
    </Modal>
      {askOpus ? (
        <OpusConfirm
          onCancel={() => setAskOpus(false)}
          onConfirm={() => {
            setAskOpus(false);
            setModel("opus");
          }}
        />
      ) : null}
    </>
  );
}
