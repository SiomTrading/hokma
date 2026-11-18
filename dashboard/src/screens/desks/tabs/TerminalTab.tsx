import { useCallback, useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { Button, EmptyState, Loading, Pane } from "@/design";
import { useI18n } from "@/i18n";
import { AssetList } from "../AssetList";
import { deskConfigApi, failText } from "../configApi";
import { DeskChat } from "../DeskChat";
import { DeskChart } from "../DeskChart";
import { ChatSplitter } from "../ChatSplitter";
import { PositionsEmpty } from "../PositionsEmpty";
import { loadChatWidth, loadFocusAsset, loadFolded, saveChatWidth, saveFocusAsset, saveFolded } from "../terminalPrefs";
import { DESK_LIMITS } from "../types";
import type { TabProps } from "./types";

type Load = { state: "loading" } | { state: "error"; message: string } | { state: "ready"; assets: string[] };

/**
 * Terminal of a desk: the watchlist on the left (the one place to add and remove assets), the chart of the asset in
 * focus in the center, the chief's chat on the right (its width is dragged and remembered per desk) and the positions
 * panel under the list and the chart, which folds down. The desk remembers both choices.
 */
export function TerminalTab({ desk }: TabProps) {
  const { t } = useI18n();
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [focus, setFocusState] = useState<string | null>(() => loadFocusAsset(desk.id));
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [chatWidth, setChatWidth] = useState(() => loadChatWidth(desk.id));
  const [folded, setFolded] = useState(() => loadFolded(desk.id));

  useEffect(() => {
    setChatWidth(loadChatWidth(desk.id));
    setFolded(loadFolded(desk.id));
    setFocusState(loadFocusAsset(desk.id));
  }, [desk.id]);

  /** The asset in focus is the desk's: it is remembered here and the next visit opens on it. */
  const setFocus = (symbol: string) => {
    setFocusState(symbol);
    saveFocusAsset(desk.id, symbol);
  };

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

  const assets = load.assets;
  const current = focus && assets.includes(focus) ? focus : (assets[0] ?? null);

  const addAsset = async (raw: string) => {
    const symbol = raw.trim().toUpperCase().slice(0, DESK_LIMITS.symbolMax);
    if (!symbol || busy) return;
    if (assets.includes(symbol)) {
      setFocus(symbol);
      setAdding(false);
      setAddError(null);
      return;
    }
    if (assets.length >= DESK_LIMITS.assetsMax) {
      setAddError(t("desks.cfg.assets.max", { n: String(DESK_LIMITS.assetsMax) }));
      return;
    }
    setBusy(true);
    setAddError(null);
    try {
      const next = [...assets, symbol];
      await deskConfigApi.setAssets(desk.id, next);
      setLoad({ state: "ready", assets: next });
      setFocus(symbol);
      setAdding(false);
    } catch (cause) {
      setAddError(failText(cause, t("desks.cfg.error.assets")));
    } finally {
      setBusy(false);
    }
  };

  const removeAsset = async (symbol: string) => {
    if (busy) return;
    setBusy(true);
    setAddError(null);
    try {
      const next = assets.filter((item) => item !== symbol);
      await deskConfigApi.setAssets(desk.id, next);
      setLoad({ state: "ready", assets: next });
    } catch (cause) {
      setAddError(failText(cause, t("desks.cfg.error.assets")));
    } finally {
      setBusy(false);
    }
  };

  /** Saves the new order. The list moves at once; if the server refuses, it goes back and says so. */
  const reorderAssets = async (next: string[]) => {
    if (busy) return;
    const before = assets;
    setLoad({ state: "ready", assets: next });
    setBusy(true);
    setAddError(null);
    try {
      await deskConfigApi.reorderAssets(desk.id, next);
    } catch (cause) {
      setLoad({ state: "ready", assets: before });
      setAddError(failText(cause, t("desks.terminal.reorder.error")));
    } finally {
      setBusy(false);
    }
  };

  const resize = (width: number, final: boolean) => {
    setChatWidth(width);
    if (final) saveChatWidth(desk.id, width);
  };

  const toggleFold = () => {
    setFolded((value) => {
      saveFolded(desk.id, !value);
      return !value;
    });
  };

  return (
    <div className={folded ? "dterm folded" : "dterm"} style={{ "--chat-w": `${chatWidth}px` } as CSSProperties}>
      <AssetList
        symbols={assets}
        focus={current}
        onFocus={setFocus}
        adding={adding}
        busy={busy}
        error={addError}
        onToggleAdd={() => {
          setAdding((open) => !open);
          setAddError(null);
        }}
        onAdd={(symbol) => void addAsset(symbol)}
        onRemove={(symbol) => void removeAsset(symbol)}
        onReorder={(next) => void reorderAssets(next)}
      />
      {current ? (
        <DeskChart symbol={current} deskId={desk.id} />
      ) : (
        <Pane className="dterm-chart" aria-label={t("desks.terminal.chartEmpty")}>
          <div className="dterm-center">
            <EmptyState
              icon="chart"
              title={t("desks.terminal.noAssets.title")}
              description={t("desks.terminal.noAssets.text")}
              action={<Button variant="primary" onClick={() => setAdding(true)}>{t("desks.terminal.add")}</Button>}
            />
          </div>
        </Pane>
      )}
      <ChatSplitter width={chatWidth} onChange={resize} />
      <DeskChat desk={desk} />
      <PositionsEmpty folded={folded} onToggle={toggleFold} />
    </div>
  );
}
