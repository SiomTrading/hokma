import { useCallback, useEffect, useRef, useState } from "react";
import { getModelStatus, retryModel } from "./modelApi";
import type { ModelStatus } from "./modelApi";

/** How often the state is read while the model is not ready. */
export const MODEL_POLL_MS = 2000;

/**
 * Follows the search model until it is ready. `status` stays null while unknown (first read, or the app could not
 * answer): the screen then shows nothing about it. Polling stops by itself once the state is `ready`.
 */
export function useModelStatus(pollMs: number = MODEL_POLL_MS): { status: ModelStatus | null; retry: () => Promise<void> } {
  const [status, setStatus] = useState<ModelStatus | null>(null);
  const alive = useRef(true);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const read = useRef<() => Promise<void>>(async () => undefined);

  const schedule = useCallback(
    (next: ModelStatus | null) => {
      if (timer.current) clearTimeout(timer.current);
      timer.current = null;
      if (!alive.current || next?.state === "ready") return;
      timer.current = setTimeout(() => void read.current(), pollMs);
    },
    [pollMs],
  );

  read.current = async () => {
    let next: ModelStatus | null = null;
    try {
      next = await getModelStatus();
    } catch {
      next = null;
    }
    if (!alive.current) return;
    if (next) setStatus(next);
    schedule(next ?? status);
  };

  useEffect(() => {
    alive.current = true;
    void read.current();
    return () => {
      alive.current = false;
      if (timer.current) clearTimeout(timer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pollMs]);

  const retry = useCallback(async () => {
    try {
      const next = await retryModel();
      if (!alive.current) return;
      setStatus(next);
      schedule(next);
    } catch {
      // A 409 means it is no longer failed: read the real state.
      await read.current();
    }
  }, [schedule]);

  return { status, retry };
}
