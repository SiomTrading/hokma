import { useCallback, useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { ApiError } from "@/api";
import { Button, Dot, Icon, IconButton, Meter, Pill } from "@/design";
import { useI18n } from "@/i18n";
import type { VoiceRecorderProps } from "../voiceTypes";
import { getVoiceStatus, startModelDownload, transcribeAudio } from "./voiceApi";
import type { VoiceLanguage, VoiceStatus } from "./voiceApi";

/** Longest recording. At about 1 MB per minute it stays far below the 25 MB upload limit. */
export const MAX_RECORDING_SECONDS = 10 * 60;
const POLL_MS = 1000;
const MIME_CANDIDATES = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];

type Phase = "idle" | "checking" | "prompt" | "downloading" | "recording" | "transcribing";

export interface VoiceRecorderComponentProps extends VoiceRecorderProps {
  /** `auto` detects any language; `es_en` (default) detects between Spanish and English; or force `es` or `en`. */
  language?: VoiceLanguage;
}

export function formatClock(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function pickMimeType(): string | undefined {
  if (typeof MediaRecorder === "undefined" || typeof MediaRecorder.isTypeSupported !== "function") return undefined;
  return MIME_CANDIDATES.find((type) => MediaRecorder.isTypeSupported(type));
}

const strip: CSSProperties = { display: "flex", alignItems: "center", gap: 8, flex: "none" };
const popover: CSSProperties = {
  position: "absolute",
  bottom: "calc(100% + 10px)",
  right: 0,
  width: 300,
  padding: 14,
  display: "flex",
  flexDirection: "column",
  gap: 10,
  background: "var(--pop)",
  border: "1px solid var(--border)",
  borderRadius: 14,
  boxShadow: "var(--shadow-menu)",
  zIndex: 20,
};

function megabytes(bytes: number): string {
  return String(Math.round(bytes / 1_000_000));
}

export function VoiceRecorder({ onTranscribed, onError, disabled, language }: VoiceRecorderComponentProps) {
  const { t } = useI18n();
  const [phase, setPhase] = useState<Phase>("idle");
  const [seconds, setSeconds] = useState(0);
  const [status, setStatus] = useState<VoiceStatus | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const alive = useRef(true);
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const chunks = useRef<Blob[]>([]);
  const cancelled = useRef(false);
  const ticker = useRef<ReturnType<typeof setInterval> | null>(null);
  const poller = useRef<ReturnType<typeof setInterval> | null>(null);
  const pending = useRef<Blob | null>(null);
  const callbacks = useRef({ onTranscribed, onError });
  callbacks.current = { onTranscribed, onError };

  const stopTicker = () => {
    if (ticker.current) clearInterval(ticker.current);
    ticker.current = null;
  };
  const stopPoller = () => {
    if (poller.current) clearInterval(poller.current);
    poller.current = null;
  };
  const releaseMic = () => {
    stream.current?.getTracks().forEach((track) => track.stop());
    stream.current = null;
  };

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      cancelled.current = true;
      stopTicker();
      stopPoller();
      if (recorder.current && recorder.current.state !== "inactive") recorder.current.stop();
      releaseMic();
    };
  }, []);

  const fail = useCallback((message?: string) => {
    if (!alive.current) return;
    setPhase("idle");
    callbacks.current.onError(message);
  }, []);

  const send = useCallback(
    async (blob: Blob) => {
      setPhase("transcribing");
      try {
        const result = await transcribeAudio(blob, language);
        if (!alive.current) return;
        pending.current = null;
        if (!result.text.trim()) {
          fail(t("voice.error.no_speech"));
          return;
        }
        setPhase("idle");
        callbacks.current.onTranscribed(result.text, result.attachment_id);
      } catch (error) {
        if (!alive.current) return;
        if (error instanceof ApiError && error.code === "model_missing") {
          pending.current = blob;                      // keep the recording: it is sent when the download ends
          await beginDownload();
          return;
        }
        pending.current = null;
        fail(error instanceof ApiError ? error.message : t("voice.error.generic"));
      }
    },
    // beginDownload is declared below and only read at call time
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [language, fail, t],
  );

  const watchDownload = useCallback(() => {
    stopPoller();
    poller.current = setInterval(async () => {
      try {
        const next = await getVoiceStatus();
        if (!alive.current) return;
        setStatus(next);
        if (next.state === "ready") {
          stopPoller();
          const blob = pending.current;
          if (blob) await send(blob);
          else {
            setPhase("idle");
            setNotice(t("voice.ready"));
          }
        } else if (next.state === "error") {
          stopPoller();
          setPhase("prompt");
        }
      } catch (error) {
        if (!alive.current) return;
        stopPoller();
        setPhase("idle");
        fail(error instanceof ApiError ? error.message : t("voice.error.generic"));
      }
    }, POLL_MS);
  }, [fail, send, t]);

  async function beginDownload() {
    setPhase("downloading");
    try {
      const next = await startModelDownload();
      if (!alive.current) return;
      setStatus(next);
      if (next.state === "ready") {
        const blob = pending.current;
        if (blob) await send(blob);
        else setPhase("idle");
        return;
      }
      watchDownload();
    } catch (error) {
      if (!alive.current) return;
      pending.current = null;
      fail(error instanceof ApiError ? error.message : t("voice.error.generic"));
    }
  }

  async function startRecording() {
    if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      fail(t("voice.error.mic_unsupported"));
      return;
    }
    let media: MediaStream;
    try {
      media = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (error) {
      const name = error instanceof DOMException ? error.name : "";
      fail(
        name === "NotAllowedError" || name === "SecurityError"
          ? t("voice.error.mic_denied")
          : name === "NotFoundError"
            ? t("voice.error.mic_missing")
            : t("voice.error.mic_failed"),
      );
      return;
    }
    if (!alive.current) {
      media.getTracks().forEach((track) => track.stop());
      return;
    }
    try {
      const mimeType = pickMimeType();
      const rec = new MediaRecorder(media, mimeType ? { mimeType } : undefined);
      stream.current = media;
      recorder.current = rec;
      chunks.current = [];
      cancelled.current = false;
      rec.ondataavailable = (event) => {
        if (event.data && event.data.size > 0) chunks.current.push(event.data);
      };
      rec.onstop = () => {
        stopTicker();
        releaseMic();
        const parts = chunks.current;
        chunks.current = [];
        recorder.current = null;
        if (cancelled.current || !alive.current) return;
        const blob = new Blob(parts, { type: rec.mimeType || mimeType || "audio/webm" });
        if (blob.size === 0) {
          fail(t("voice.error.mic_failed"));
          return;
        }
        void send(blob);
      };
      rec.onerror = () => {
        cancelled.current = true;
        stopTicker();
        releaseMic();
        fail(t("voice.error.mic_failed"));
      };
      rec.start(1000);
      const startedAt = Date.now();
      setSeconds(0);
      setPhase("recording");
      ticker.current = setInterval(() => {
        const elapsed = (Date.now() - startedAt) / 1000;
        setSeconds(elapsed);
        if (elapsed >= MAX_RECORDING_SECONDS) stopRecording();
      }, 250);
    } catch {
      media.getTracks().forEach((track) => track.stop());
      fail(t("voice.error.mic_failed"));
    }
  }

  function stopRecording() {
    const rec = recorder.current;
    if (rec && rec.state !== "inactive") rec.stop();
  }

  function cancelRecording() {
    cancelled.current = true;
    stopTicker();
    const rec = recorder.current;
    if (rec && rec.state !== "inactive") rec.stop();
    else releaseMic();
    chunks.current = [];
    setPhase("idle");
  }

  async function onMicrophone() {
    setNotice(null);
    setPhase("checking");
    try {
      const current = await getVoiceStatus();
      if (!alive.current) return;
      setStatus(current);
      if (!current.available) {
        fail(t("voice.error.engine_missing"));
        return;
      }
      if (current.state === "ready") {
        await startRecording();
      } else if (current.state === "downloading") {
        setPhase("downloading");
        watchDownload();
      } else {
        setPhase("prompt");
      }
    } catch (error) {
      if (!alive.current) return;
      fail(error instanceof ApiError ? error.message : t("voice.error.generic"));
    }
  }

  function closePrompt() {
    pending.current = null;
    stopPoller();
    setPhase("idle");
    setNotice(null);
  }

  const busy = phase === "checking" || phase === "transcribing";

  if (phase === "recording") {
    return (
      <div style={strip} role="group" aria-label={t("voice.recording")}>
        <Dot live color="var(--bad)" />
        <span className="mono" role="timer" aria-label={t("voice.recording")} style={{ fontSize: 12, color: "var(--t1)", minWidth: 40 }}>
          {formatClock(seconds)}
        </span>
        <IconButton icon="close" label={t("voice.cancel")} onClick={cancelRecording} />
        <IconButton icon="send" label={t("voice.stop")} on onClick={stopRecording} />
      </div>
    );
  }

  if (phase === "transcribing") {
    return (
      <div style={strip} role="status" aria-live="polite">
        <span className="spin" aria-hidden="true" />
        <span className="p">{t("voice.transcribing")}</span>
      </div>
    );
  }

  const showPopover = phase === "prompt" || phase === "downloading" || notice !== null;
  const failed = phase === "prompt" && status?.state === "error";
  const size = status?.download_mb ? String(status.download_mb) : "500";
  const percent = Math.round((status?.progress ?? 0) * 100);

  return (
    <div style={{ position: "relative", flex: "none", display: "flex" }}>
      <IconButton
        icon="mic"
        label={phase === "checking" ? t("voice.checking") : t("voice.start")}
        disabled={disabled || busy || phase === "downloading"}
        aria-busy={busy}
        onClick={() => void onMicrophone()}
      />
      {showPopover ? (
        <div style={popover} role={failed ? "alert" : "status"} aria-live="polite">
          {phase === "downloading" ? (
            <>
              <span className="h2">{t("voice.downloading")}</span>
              <Meter value={percent} label={t("voice.downloading")} />
              <span className="p mono">
                {status && status.bytes_total > 0
                  ? t("voice.downloading.progress", { done: megabytes(status.bytes_done), total: megabytes(status.bytes_total) })
                  : "..."}{" "}
                ({percent}%)
              </span>
              <span className="p">{t("voice.downloading.hint")}</span>
            </>
          ) : phase === "prompt" ? (
            <>
              {failed ? <Pill tone="bad">{t("voice.error.title")}</Pill> : <span className="h2">{t("voice.prompt.title")}</span>}
              <p className="p">{failed && status?.error ? status.error.message : t("voice.prompt.text", { size })}</p>
              <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                <Button size="sm" variant="ghost" onClick={closePrompt}>
                  {t("voice.prompt.close")}
                </Button>
                <Button size="sm" variant="primary" onClick={() => void beginDownload()}>
                  {failed ? t("voice.retry") : t("voice.prompt.download")}
                </Button>
              </div>
            </>
          ) : (
            <>
              <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <Icon name="mic" size={14} />
                <span className="p">{notice}</span>
              </span>
              <div style={{ display: "flex", justifyContent: "flex-end" }}>
                <Button size="sm" variant="ghost" onClick={() => setNotice(null)}>
                  {t("voice.dismiss")}
                </Button>
              </div>
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}
