import { useEffect, useState } from "react";
import { Button } from "@/design";
import { useI18n } from "@/i18n";
import { connectionsApi } from "./api";
import { SCHWAB_LOGIN_HASH } from "./focus";
import { expiresAt, whenText } from "./session";
import type { SchwabSession } from "./types";

interface Props {
  session: SchwabSession | null | undefined;
  onLogin: () => void;
}

/** The notice of 4.3 rule 8: the Schwab session ends within 24 hours, or it already ended. Nothing shows while it is fine. */
export function SchwabBand({ session, onLogin }: Props) {
  const { t, locale } = useI18n();
  if (!session || (session.state !== "expiring" && session.state !== "expired")) return null;
  const date = expiresAt(session);
  const text = session.state === "expired" || !date ? t("connections.schwab.expired") : t("connections.schwab.expiresSoon", { when: whenText(date, locale) });
  return (
    <div className="conn-banner conn-session" role="status" data-testid="schwab-band" data-state={session.state}>
      <span>{text}</span>
      <Button size="sm" variant="primary" onClick={onLogin}>
        {t("connections.schwab.login")}
      </Button>
    </div>
  );
}

/** The same notice on the home screen. It reads the list once; the button goes to Connections with the sign-in step open. */
export function HomeSchwabBand() {
  const [session, setSession] = useState<SchwabSession | null>(null);
  useEffect(() => {
    let alive = true;
    connectionsApi.list().then(
      (data) => {
        if (alive) setSession(data.connections.find((card) => card.id === "schwab")?.session ?? null);
      },
      () => undefined, // no notice is better than an error on the home screen
    );
    return () => {
      alive = false;
    };
  }, []);
  return <SchwabBand session={session} onLogin={() => void (window.location.hash = SCHWAB_LOGIN_HASH)} />;
}
