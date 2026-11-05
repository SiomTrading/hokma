import { useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import type { ClipboardEvent, KeyboardEvent, Ref } from "react";
import { Button, Icon, IconButton, Menu, MenuItem } from "@/design";
import { useI18n } from "@/i18n";
import { useShell } from "@/shell/useShell";
import { MAX_ATTACHMENTS, MAX_ATTACHMENT_BYTES, USAGE_WARN_PCT, formatSize, uploadAttachment } from "./chatApi";
import { suggestionsFor } from "./commands";
import type { Suggestion } from "./commands";
import type { AttachmentChip } from "./state";
import { nextKey } from "./state";
import { MODELS } from "./types";
import type { ChatOptions, ModelId } from "./types";
import { voice } from "./voiceTypes";

export interface ComposerHandle {
  addFiles: (files: File[]) => void;
  focus: () => void;
}

interface ComposerProps {
  ref?: Ref<ComposerHandle>;
  model: ModelId;
  running: boolean;
  stopping: boolean;
  options: ChatOptions | null;
  /** Bumps each time the engine asks to open the model menu (`/model` with no argument). */
  modelMenuRequest: number;
  /** Share of the weekly cap used (0 to 100); null when there is no cap or no answer yet. */
  usagePct?: number | null;
  onModel: (model: ModelId) => void;
  onSend: (text: string, attachments: AttachmentChip[]) => boolean;
  onStop: () => void;
}

const MODEL_DOT: Record<ModelId, string> = { haiku: "var(--ok)", sonnet: "var(--acc)", opus: "var(--org)" };

export function Composer({ ref, model, running, stopping, options, modelMenuRequest, usagePct = null, onModel, onSend, onStop }: ComposerProps) {
  const { t } = useI18n();
  const shell = useShell();
  const [text, setText] = useState("");
  const [chips, setChips] = useState<AttachmentChip[]>([]);
  const [modelMenu, setModelMenu] = useState(false);
  const [active, setActive] = useState(0);
  const [dismissedFor, setDismissedFor] = useState<string | null>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const file = useRef<HTMLInputElement>(null);
  const pill = useRef<HTMLButtonElement>(null);
  const chipsRef = useRef(chips);
  chipsRef.current = chips;

  useEffect(() => {
    if (modelMenuRequest > 0) setModelMenu(true);
  }, [modelMenuRequest]);

  useEffect(() => {
    const el = input.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 168)}px`;
  }, [text]);

  const patchChip = useCallback((key: string, patch: Partial<AttachmentChip>) => {
    setChips((list) => list.map((chip) => (chip.key === key ? { ...chip, ...patch } : chip)));
  }, []);

  const addFiles = useCallback(
    (files: File[]) => {
      if (files.length === 0) return;
      let room = MAX_ATTACHMENTS - chipsRef.current.length;
      for (const f of files) {
        if (room <= 0) {
          shell.toast({ tone: "warn", text: t("chat.composer.tooMany", { max: MAX_ATTACHMENTS }) });
          break;
        }
        if (f.size > MAX_ATTACHMENT_BYTES) {
          shell.toast({ tone: "warn", text: t("chat.composer.tooLarge", { name: f.name || "file" }) });
          continue;
        }
        room -= 1;
        const isImage = /^image\/(png|jpeg|gif|webp)$/.test(f.type);
        const chip: AttachmentChip = {
          key: nextKey("c"),
          name: f.name || (isImage ? "image.png" : "file"),
          size: f.size,
          kind: isImage ? "image" : "file",
          mime: f.type,
          previewUrl: isImage && typeof URL.createObjectURL === "function" ? URL.createObjectURL(f) : undefined,
          status: "uploading",
        };
        setChips((list) => [...list, chip]);
        uploadAttachment(f.name ? f : new File([f], chip.name, { type: f.type })).then(
          (done) => patchChip(chip.key, { id: done.id, kind: done.kind, mime: done.mime, size: done.size, status: "ready" }),
          () => patchChip(chip.key, { status: "error" }),
        );
      }
    },
    [patchChip, shell, t],
  );

  useImperativeHandle(ref, () => ({ addFiles, focus: () => input.current?.focus() }), [addFiles]);

  const removeChip = (key: string) => {
    const gone = chipsRef.current.find((chip) => chip.key === key);
    if (gone?.previewUrl && typeof URL.revokeObjectURL === "function") URL.revokeObjectURL(gone.previewUrl);
    setChips((list) => list.filter((chip) => chip.key !== key));
  };

  const suggestions: Suggestion[] = useMemo(() => {
    if (dismissedFor === text) return [];
    return suggestionsFor(text, t, options) ?? [];
  }, [text, t, options, dismissedFor]);
  const menuOpen = suggestions.length > 0 && !running;
  const showNoMatches = !running && dismissedFor !== text && text.startsWith("/") && /^\/[A-Za-z]+\s+\S/.test(text) && suggestions.length === 0 && suggestionsFor(text, t, options) !== null;

  useEffect(() => setActive(0), [text]);

  const submit = (override?: string) => {
    const value = (override ?? text).trim();
    const ready = chips.filter((chip) => chip.status === "ready");
    if (chips.some((chip) => chip.status === "uploading")) {
      shell.toast({ tone: "warn", text: t("chat.composer.waitUpload") });
      return;
    }
    if (!value && ready.length === 0) return;
    if (running) return;
    if (onSend(value, ready)) {
      setText("");
      setChips([]);
    }
  };

  const pick = (s: Suggestion) => {
    if (s.run) {
      submit(s.value);
    } else {
      setText(s.value);
      input.current?.focus();
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (menuOpen) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        setActive((i) => (event.key === "ArrowDown" ? (i + 1) % suggestions.length : (i - 1 + suggestions.length) % suggestions.length));
        return;
      }
      const complete = suggestions.some((s) => !s.run && s.value.trim() === text.trim());
      if (event.key === "Tab" || (event.key === "Enter" && !event.shiftKey && !complete)) {
        event.preventDefault();
        pick(suggestions[active]!);
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  const onPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(event.clipboardData?.files ?? []);
    if (files.length === 0) return;
    event.preventDefault();
    addFiles(files);
  };

  const sendDisabled = running || (!text.trim() && chips.every((chip) => chip.status !== "ready"));

  return (
    <div className="chat-composer">
      {menuOpen ? (
        <Menu open onClose={() => setDismissedFor(text)} label={t("chat.cmd.menu")} className="chat-suggest">
          {suggestions.map((s, index) => (
            <MenuItem key={s.key} selected={index === active} dotColor={s.dot} hint={s.hint} onMouseDown={(e) => e.preventDefault()} onClick={() => pick(s)}>
              {s.label}
            </MenuItem>
          ))}
        </Menu>
      ) : null}
      {showNoMatches ? (
        <div className="menu chat-suggest" role="status">
          <span className="p" style={{ padding: "8px 10px", display: "block" }}>
            {t("chat.cmd.noMatches")}
          </span>
        </div>
      ) : null}
      <Menu open={modelMenu} onClose={() => setModelMenu(false)} label={t("chat.model.menu")} triggerRef={pill}>
        <div className="chat-menu-head">
          <span className="lbl">{t("chat.model.provider")}</span>
          <span className="pill" style={{ height: 20, fontSize: 10 }}>
            {t("chat.model.plan")}
          </span>
        </div>
        {MODELS.map((m) => (
          <MenuItem
            key={m}
            selected={m === model}
            dotColor={MODEL_DOT[m]}
            hint={t(`chat.model.${m}.hint`)}
            onClick={() => {
              onModel(m);
              setModelMenu(false);
              input.current?.focus();
            }}
          >
            {t(`chat.model.${m}`)}
          </MenuItem>
        ))}
      </Menu>

      {chips.length > 0 ? (
        <ul className="chat-pending" aria-label={t("chat.composer.attach")}>
          {chips.map((chip) => (
            <li key={chip.key} className={`chip chat-chip ${chip.status === "error" ? "is-error" : ""}`} data-testid="pending-attachment">
              {chip.kind === "image" && chip.previewUrl ? <img className="chat-thumb" src={chip.previewUrl} alt="" /> : <Icon name={chip.transcribed ? "mic" : chip.kind === "image" ? "image" : "file"} size={14} />}
              <span className="chat-chip-name">{chip.name}</span>
              {chip.transcribed ? (
                <span className="pill ok">{t("chat.voice.transcribed")}</span>
              ) : chip.status === "uploading" ? (
                <span className="pill">{t("chat.composer.uploading")}</span>
              ) : chip.status === "error" ? (
                <span className="pill bad">{t("chat.composer.uploadFailed")}</span>
              ) : chip.size > 0 ? (
                <span className="chat-small">{formatSize(chip.size)}</span>
              ) : null}
              <button type="button" className="chat-x" aria-label={t("chat.composer.removeAttachment", { name: chip.name })} onClick={() => removeChip(chip.key)}>
                ×
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      <div className="pane chat-bar">
        <IconButton icon="plus" label={t("chat.composer.attach")} onClick={() => file.current?.click()} />
        <input
          ref={file}
          type="file"
          multiple
          hidden
          data-testid="file-input"
          onChange={(event) => {
            addFiles(Array.from(event.target.files ?? []));
            event.target.value = "";
          }}
        />
        <label htmlFor="chat-message" className="sr-only">
          {t("chat.composer.label")}
        </label>
        <textarea
          id="chat-message"
          ref={input}
          className="in chat-input"
          rows={1}
          value={text}
          placeholder={t("chat.composer.placeholder")}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
        />
        <button ref={pill} type="button" className="pill acc chat-model" aria-haspopup="menu" aria-expanded={modelMenu} onClick={() => setModelMenu((v) => !v)}>
          {t("chat.model.pill", { name: t(`chat.model.${model}`) })}
          <Icon name="chevronUp" size={12} strokeWidth={2.4} />
        </button>
        {voice.Recorder ? (
          <voice.Recorder
            disabled={running}
            onTranscribed={(spoken, attachmentId) => {
              setText((current) => (current && !current.endsWith(" ") ? `${current} ${spoken}` : `${current}${spoken}`));
              setChips((list) => [
                ...list,
                { key: nextKey("c"), id: attachmentId, name: t("chat.voice.note"), size: 0, kind: "file", mime: "audio/*", status: "ready", transcribed: true },
              ]);
              input.current?.focus();
            }}
            onError={(message) => shell.toast({ tone: "bad", text: message || t("chat.voice.error") })}
          />
        ) : null}
        {running ? (
          <Button variant="danger" size="sm" disabled={stopping} onClick={onStop}>
            {stopping ? t("chat.composer.stopping") : t("chat.composer.stop")}
          </Button>
        ) : (
          <button type="button" className="ib chat-send" aria-label={t("chat.composer.send")} disabled={sendDisabled} onClick={() => submit()}>
            <Icon name="send" size={16} strokeWidth={2.2} />
          </button>
        )}
      </div>
      <div className="chat-help">
        <span className="p">{t(`chat.model.${model}.help`)}</span>
        {usagePct !== null ? (
          <div className="chat-usage" data-testid="usage-bar" data-over={usagePct >= USAGE_WARN_PCT}>
            <div
              className="chat-usage-bar"
              role="progressbar"
              aria-label={t("chat.usage.label")}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(Math.min(usagePct, 100))}
            >
              <span
                className="chat-usage-fill"
                style={{ width: `${Math.min(Math.max(usagePct, 0), 100)}%`, background: usagePct >= USAGE_WARN_PCT ? "var(--org)" : "var(--acc)" }}
              />
            </div>
            <span>{t("chat.usage.value", { pct: Math.round(usagePct) })}</span>
          </div>
        ) : null}
      </div>
    </div>
  );
}
