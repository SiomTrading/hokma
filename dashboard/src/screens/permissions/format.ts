export { formatSize } from "@/screens/chat/chatApi";

/** "5 Nov 2026, 13:45" in the language of the app; the raw text when it is not a date. */
export function formatWhen(iso: string, locale: string): string {
  const date = new Date(iso.includes("T") || iso.endsWith("Z") ? iso : iso.replace(" ", "T") + "Z");
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString(locale, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}
