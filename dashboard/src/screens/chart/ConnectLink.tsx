import { useI18n } from "@/i18n";
import { connectionsHref, requestFocus } from "@/screens/connections/focus";
import { useShell } from "@/shell/useShell";

/** "Connect a live source": opens Connections with the group of the symbol's asset class in front. */
export function ConnectLink({ assetClass }: { assetClass: string | null }) {
  const { t } = useI18n();
  const shell = useShell();
  return (
    <a
      className="live-link"
      href={connectionsHref(assetClass)}
      data-testid="connect-link"
      onClick={(event) => {
        event.preventDefault();
        requestFocus(assetClass);
        shell.openScreen("connections");
      }}
    >
      {t("chart.live.connect")}
    </a>
  );
}
