import { useCallback, useEffect, useState } from "react";
import { Button, EmptyState, Loading } from "@/design";
import { useI18n } from "@/i18n";
import { AssetList } from "../AssetList";
import { deskConfigApi, failText } from "../configApi";
import { DeskChat } from "../DeskChat";
import { DeskChart } from "../DeskChart";
import { PositionsEmpty } from "../PositionsEmpty";
import type { TabProps } from "./types";

type Load = { state: "loading" } | { state: "error"; message: string } | { state: "ready"; assets: string[] };

/**
 * Terminal of a desk: assets on the left, the chart of the asset in focus in the center, the chief's chat on the right
 * (top to bottom) and the positions panel under the list and the chart. Fixed grid, no movable panels.
 */
export function TerminalTab({ desk }: TabProps) {
  const { t } = useI18n();
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [focus, setFocus] = useState<string | null>(null);

  const read = useCallback(() => {
    setLoad({ state: "loading" });
    deskConfigApi
      .detail(desk.id)
      .then((detail) => setLoad({ state: "ready", assets: detail.assets }))
      .catch((cause) => setLoad({ state: "error", message: failText(cause, t("desks.terminal.error")) }));
  }, [desk.id, t]);

  useEffect(read, [read]);

  if (load.state === "loading") {
    return (
      <div className="dterm-whole">
        <Loading label={t("desks.terminal.loading")} />
      </div>
    );
  }
  if (load.state === "error") {
    return (
      <div className="dterm-whole" role="alert">
        <EmptyState
          icon="terminal"
          title={t("desks.terminal.error")}
          description={load.message}
          action={<Button onClick={read}>{t("ui.error.retry")}</Button>}
        />
      </div>
    );
  }
  if (load.assets.length === 0) {
    return (
      <div className="dterm-whole">
        <EmptyState icon="chart" title={t("desks.terminal.noAssets.title")} description={t("desks.terminal.noAssets.text")} />
      </div>
    );
  }

  const current = focus && load.assets.includes(focus) ? focus : load.assets[0]!;
  return (
    <div className="dterm">
      <AssetList symbols={load.assets} focus={current} onFocus={setFocus} />
      <DeskChart symbol={current} />
      <DeskChat desk={desk} />
      <PositionsEmpty />
    </div>
  );
}
