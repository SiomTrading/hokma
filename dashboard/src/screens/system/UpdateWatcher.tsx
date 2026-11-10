import { useEffect } from "react";
import { learnEngineVersion, useToken } from "@/api";
import { refreshUpdateStatus } from "./updateStatus";

const FIRST_AGAIN_MS = 15_000;      // the engine searches a few seconds after it starts
const EVERY_MS = 10 * 60_000;

/** Keeps the "new version" dot current. Renders nothing. */
export function UpdateWatcher() {
  const token = useToken();
  useEffect(() => {
    if (!token) return;
    void learnEngineVersion();          // a later crash watch reloads the page if the engine comes back on another version
    void refreshUpdateStatus();
    const early = setTimeout(() => void refreshUpdateStatus(), FIRST_AGAIN_MS);
    const every = setInterval(() => void refreshUpdateStatus(), EVERY_MS);
    return () => {
      clearTimeout(early);
      clearInterval(every);
    };
  }, [token]);
  return null;
}
