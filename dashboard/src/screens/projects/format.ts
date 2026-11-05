import type { ProjectKind, TreeEntry } from "./types";

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatDuration(seconds: number): string {
  const total = Math.round(seconds);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** Duration for audio that Hokma can measure, size for everything else. */
export function entryMeta(entry: TreeEntry): string {
  if (entry.type === "dir") return "";
  if (typeof entry.duration === "number") return formatDuration(entry.duration);
  return formatSize(entry.size ?? 0);
}

const IMAGE = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "heic", "svg"]);
const AUDIO = new Set(["mp3", "m4a", "wav", "ogg", "flac", "aac", "opus", "webm"]);

export function iconFor(entry: TreeEntry): "folder" | "image" | "mic" | "file" {
  if (entry.type === "dir") return "folder";
  const ext = entry.name.includes(".") ? entry.name.split(".").pop()!.toLowerCase() : "";
  return IMAGE.has(ext) ? "image" : AUDIO.has(ext) ? "mic" : "file";
}

export const KIND_COLOR: Record<ProjectKind, string> = {
  ips: "var(--acc)",
  wealth: "var(--ok)",
  strategy: "var(--org)",
  research: "var(--warn)",
  robot: "var(--acc-h)",
  other: "var(--t3)",
};
