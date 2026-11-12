import { useCallback, useEffect, useState } from "react";
import { enginesApi, type ActiveEngine, type Connection, type EnginesListing } from "./api";

export function useEngines(conversationId?: string | null) {
  const [data, setData] = useState<EnginesListing | null>(null);
  const [failed, setFailed] = useState(false);
  const reload = useCallback(async () => {
    try {
      setData(await enginesApi.list(conversationId));
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, [conversationId]);
  useEffect(() => {
    void reload();
  }, [reload]);
  // The server says which engine will answer (the rule of the chat); the saved default is only the answer of an old server.
  const saved: Connection | null = data?.connections?.find((c) => c.is_default && c.status !== "removed") ?? null;
  const active: (Pick<ActiveEngine, "engine"> & Partial<ActiveEngine>) | null = data?.active ?? saved;
  return { data, failed, reload, active };
}
