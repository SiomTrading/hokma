import { Button, EmptyState, Loading } from "@/design";
import { useI18n } from "@/i18n";
import type { ScreenProps } from "@/shell/registry";
import { useShell } from "@/shell/useShell";
import { DeskScreen } from "./DeskScreen";
import { FundView } from "./FundView";
import { useDesks } from "./store";
import "./desks.css";

/**
 * The "Mesas de dinero" window. Without a `desk` param it shows the Fund; with one it shows that desk and its five
 * tabs. `tab` in the params only picks the tab it opens on. `new=1` opens the Fund with the "create a desk" flow open.
 */
export function DesksScreen({ tabKey, params }: ScreenProps) {
  const { t } = useI18n();
  const shell = useShell();
  const { desks, loaded } = useDesks();

  const deskId = params.desk;
  const desk = deskId ? desks.find((item) => String(item.id) === deskId) : undefined;

  if (!deskId) return <FundView openCreate={params.new === "1"} />;

  if (!loaded) {
    return (
      <section className="desks-screen">
        <Loading label={t("desks.fund.loading")} />
      </section>
    );
  }

  if (!desk) {
    return (
      <section className="desks-screen">
        <EmptyState
          icon="desks"
          title={t("desks.notFound.title")}
          description={t("desks.notFound.text")}
          action={<Button onClick={() => shell.openScreen("desks")}>{t("desks.notFound.back")}</Button>}
        />
      </section>
    );
  }

  return <DeskScreen key={desk.id} desk={desk} desks={desks} tabKey={tabKey} initialTab={params.tab} />;
}
