import { api } from "@/api";

export type VoiceLanguage = "auto" | "es_en" | "es" | "en";

export interface VoiceStatus {
  /** False when the speech engine is not available on this computer. */
  available: boolean;
  model: string;
  state: "missing" | "downloading" | "ready" | "error";
  /** 0 to 1. */
  progress: number;
  bytes_done: number;
  bytes_total: number;
  /** Approximate size of the one-time download, known before it starts. */
  download_mb?: number;
  /** Set when state is "error": a localized, user-safe message. */
  error: { code: string; message: string; detail?: string } | null;
}

export interface TranscribeResult {
  text: string;
  attachment_id: string;
  name: string;
  mime: string;
  size: number;
  language: string;
  duration: number;
}

export const getVoiceStatus = () => api.get<VoiceStatus>("/api/transcribe/status");

export const startModelDownload = () => api.post<VoiceStatus>("/api/transcribe/model");

/** Sends the recording. The audio only goes to this computer (127.0.0.1). No timeout: transcription runs on the CPU. */
export function transcribeAudio(blob: Blob, language?: VoiceLanguage): Promise<TranscribeResult> {
  const query = language ? `?language=${language}` : "";
  return api.post<TranscribeResult>(`/api/transcribe${query}`, blob, { timeoutMs: 0 });
}
