import { getHub } from "../chat/hub";
import type { AnalysisEvent } from "./analysisApi";

/**
 * The live progress of the analyses of one desk. It asks the chat socket to send them (`watch_desk`, again every time the socket opens) and hands
 * every `analysis` message of that desk to `listener`. There is no timer and no polling: what the run does arrives pushed. A push that came while the
 * socket was down is lost, so `onReopen` runs every time the socket opens again after it was open: the caller reads what it shows once more. Returns the way to stop.
 */
export function watchAnalysis(deskId: number, listener: (event: AnalysisEvent) => void, onReopen?: () => void): () => void {
  const hub = getHub();
  const ask = () => {
    if (hub.status === "open") hub.send({ type: "watch_desk", desk_id: deskId });
  };
  const stopWatch = hub.watch((event) => {
    const message = event as unknown as AnalysisEvent;
    if (message.type === "analysis" && message.desk_id === deskId) listener(message);
  });
  let wasOpen = hub.status === "open";
  const stopStatus = hub.onStatus((status) => {
    if (status !== "open") return;
    ask();
    if (wasOpen) onReopen?.();
    wasOpen = true;
  });
  ask();
  return () => {
    stopWatch();
    stopStatus();
  };
}
