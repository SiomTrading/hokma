import { useI18n } from "@/i18n";

/**
 * Name to show for an agent. The kit's own agents read `agents.kit.<slug>.name` from the catalog in the user's
 * language; the owner's own agents and any slug without an entry keep the name the server sent.
 */
export function useAgentName(): (slug: string, fallback: string) => string {
  const { t } = useI18n();
  return (slug, fallback) => {
    const key = `agents.kit.${slug}.name`;
    const text = t(key);
    return text === key ? fallback : text;
  };
}
