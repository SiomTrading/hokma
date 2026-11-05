import { useState } from "react";
import type { FormEvent } from "react";
import { Button, Field, Input, Modal, Select, Toggle } from "@/design";
import { useI18n } from "@/i18n";
import * as client from "./api";
import { messageOf } from "./store";
import { PROJECT_KINDS } from "./types";
import type { LinkResult, ProjectInfo, ProjectKind } from "./types";

function Actions({ busy, busyLabel, onCancel, saveLabel }: { busy: boolean; busyLabel: string; onCancel: () => void; saveLabel: string }) {
  const { t } = useI18n();
  return (
    <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 6 }}>
      <Button type="button" variant="ghost" size="sm" onClick={onCancel} disabled={busy}>
        {t("projects.cancel")}
      </Button>
      <Button type="submit" variant="primary" size="sm" disabled={busy}>
        {busy ? busyLabel : saveLabel}
      </Button>
    </div>
  );
}

const form = { display: "flex", flexDirection: "column", gap: 14, padding: 24, width: "100%" } as const;

export function CreateProjectDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (project: ProjectInfo) => void }) {
  const { t } = useI18n();
  const [name, setName] = useState("");
  const [kind, setKind] = useState<ProjectKind>("other");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim()) {
      setError(t("projects.error.name"));
      return;
    }
    setBusy(true);
    setError(undefined);
    try {
      onCreated(await client.createProject(name.trim(), kind));
    } catch (cause) {
      setError(messageOf(cause));
      setBusy(false);
    }
  };

  return (
    <Modal label={t("projects.dialog.createTitle")} onClose={onClose} style={{ width: "min(440px, 100%)" }}>
      <form onSubmit={submit} style={form}>
        <h2 className="h2">{t("projects.dialog.createTitle")}</h2>
        <Field label={t("projects.field.name")} error={error}>
          {(control) => <Input {...control} value={name} maxLength={120} autoFocus onChange={(e) => setName(e.target.value)} />}
        </Field>
        <Field label={t("projects.field.kind")}>
          {(control) => (
            <Select {...control} value={kind} onChange={(e) => setKind(e.target.value as ProjectKind)}>
              {PROJECT_KINDS.map((k) => (
                <option key={k} value={k}>
                  {t(`projects.kind.${k}`)}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <Actions busy={busy} busyLabel={t("projects.creating")} onCancel={onClose} saveLabel={t("projects.create")} />
      </form>
    </Modal>
  );
}

export function RenameProjectDialog({ project, onClose, onRenamed }: { project: ProjectInfo; onClose: () => void; onRenamed: () => void }) {
  const { t } = useI18n();
  const [name, setName] = useState(project.name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim()) {
      setError(t("projects.error.name"));
      return;
    }
    setBusy(true);
    setError(undefined);
    try {
      await client.renameProject(project.id, name.trim());
      onRenamed();
    } catch (cause) {
      setError(messageOf(cause));
      setBusy(false);
    }
  };

  return (
    <Modal label={t("projects.dialog.renameTitle")} onClose={onClose} style={{ width: "min(440px, 100%)" }}>
      <form onSubmit={submit} style={form}>
        <h2 className="h2">{t("projects.dialog.renameTitle")}</h2>
        <Field label={t("projects.field.name")} error={error}>
          {(control) => <Input {...control} value={name} maxLength={120} autoFocus onChange={(e) => setName(e.target.value)} />}
        </Field>
        <Actions busy={busy} busyLabel={t("projects.creating")} onCancel={onClose} saveLabel={t("projects.save")} />
      </form>
    </Modal>
  );
}

export function LinkDialog({ project, onClose, onAdded }: { project: ProjectInfo; onClose: () => void; onAdded: (result: LinkResult) => void }) {
  const { t } = useI18n();
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [download, setDownload] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>();

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!/^https:\/\/\S+$/i.test(url.trim())) {
      setError(t("projects.error.url"));
      return;
    }
    setBusy(true);
    setError(undefined);
    try {
      onAdded(await client.addProjectLink(project.id, url.trim(), title.trim(), download));
    } catch (cause) {
      setError(messageOf(cause));
      setBusy(false);
    }
  };

  return (
    <Modal label={t("projects.dialog.linkTitle")} onClose={onClose} style={{ width: "min(480px, 100%)" }}>
      <form onSubmit={submit} style={form}>
        <h2 className="h2">{t("projects.dialog.linkTitle")}</h2>
        <Field label={t("projects.field.url")} error={error}>
          {(control) => <Input {...control} value={url} autoFocus inputMode="url" placeholder="https://" onChange={(e) => setUrl(e.target.value)} />}
        </Field>
        <Field label={t("projects.field.title")}>
          {(control) => <Input {...control} value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />}
        </Field>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <Toggle checked={download} onChange={setDownload} label={t("projects.field.download")} />
            <span className="p">{t("projects.field.download")}</span>
          </div>
          <span className="hint">{t("projects.field.downloadHint")}</span>
        </div>
        <Actions busy={busy} busyLabel={t("projects.adding")} onCancel={onClose} saveLabel={t("projects.addLink")} />
      </form>
    </Modal>
  );
}
