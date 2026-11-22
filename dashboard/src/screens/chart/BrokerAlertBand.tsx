import type { BrokerAlert } from "./liveState";
import { ConnectLink } from "./ConnectLink";

/** The loss of the broker that serves the data (SPEC-016 3.8): red and fixed while it lasts, green for five seconds when it is back. */
export function BrokerAlertBand({ alert, assetClass }: { alert: BrokerAlert | null; assetClass: string | null }) {
  if (!alert) return null;
  return (
    <div className={"live-band " + alert.state} role="alert" data-testid="broker-alert" data-state={alert.state}>
      <span>{alert.message}</span>
      {alert.state === "lost" ? <ConnectLink assetClass={assetClass} /> : null}
    </div>
  );
}
