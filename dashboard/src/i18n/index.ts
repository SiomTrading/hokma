const messages = {
  es: { tagline: "Tu fondo de inversión personal con IA" },
  en: { tagline: "Your personal AI investment fund" },
} as const;

export type Locale = keyof typeof messages;
export type MessageKey = keyof (typeof messages)["es"];

export const DEFAULT_LOCALE: Locale = "es";

export function t(key: MessageKey, locale: Locale = DEFAULT_LOCALE): string {
  return messages[locale][key];
}
