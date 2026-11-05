import { useCallback, useState } from "react";
import type { ReactNode } from "react";
import { ApiError } from "@/api";
import { switchDatabase } from "./api";
import { ReplaceConfirm } from "./ReplaceConfirm";
import type { DbChoice, MigrationDto } from "./types";

interface Pending {
  message: string;
  engine: DbChoice;
  url: string | undefined;
}

/**
 * Moves the fund to another database engine. When the destination already holds data the backend refuses with
 * `target_not_empty`; the owner then confirms in a dialog (the backend's words) and the move runs again overwriting it.
 * `onMoved` receives the verification report, or null when the engine in use was already the chosen one.
 */
export function useDatabaseSwitch(current: DbChoice, onMoved: (report: MigrationDto | null) => void | Promise<void>, onError: (message: string) => void) {
  const [pending, setPending] = useState<Pending | null>(null);

  const attempt = useCallback(
    async (engine: DbChoice, url: string | undefined, overwrite = false): Promise<void> => {
      try {
        const report = await switchDatabase(current, engine, url, overwrite);
        setPending(null);
        await onMoved(report);
      } catch (cause) {
        if (!overwrite && cause instanceof ApiError && cause.code === "target_not_empty") {
          setPending({ message: cause.message, engine, url });
          return;
        }
        setPending(null);
        throw cause;
      }
    },
    [current, onMoved],
  );

  const dialog: ReactNode = pending ? (
    <ReplaceConfirm
      message={pending.message}
      onCancel={() => setPending(null)}
      onConfirm={() => {
        const { engine, url } = pending;
        attempt(engine, url, true).catch((cause: unknown) => onError(cause instanceof Error ? cause.message : String(cause)));
      }}
    />
  ) : null;

  return { attempt, dialog };
}
