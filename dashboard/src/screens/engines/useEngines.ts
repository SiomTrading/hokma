import { useCallback, useEffect, useState } from "react";
import { enginesApi, type Connection, type EnginesListing } from "./api";

export function useEngines() {
  const [data, setData] = useState<EnginesListing | null>(null);
  const [failed, setFailed] = useState(false);
  const reload = useCallback(async () => {
    try {
      setData(await enginesApi.list());
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, []);
  useEffect(() => {
    void reload();
  }, [reload]);
  const active: Connection | null = data?.connections?.find((c) => c.is_default && c.status !== "removed") ?? null;
  return { data, failed, reload, active };
}

/** True when the active engine is not Claude Code: the chat then shows levels instead of model names. */
export function useTierMode(): boolean {
  const { active } = useEngines();
  return active !== null && active.engine !== "claude_code";
}
