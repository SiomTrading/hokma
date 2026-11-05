import { registerScreen, registerSlot } from "@/shell/registry";
import { useShell } from "@/shell/useShell";
import { AssetSearch } from "./AssetSearch";
import { ChartScreen } from "./ChartScreen";

/** "+" menu > Assets: search and open a symbol in its own tab. */
function AssetsMenu({ close }: { close: () => void }) {
  const shell = useShell();
  return (
    <AssetSearch
      compact
      onPick={(symbol) => {
        shell.openScreen("chart", { symbol });
        close();
      }}
    />
  );
}

// keepAlive: the timeframe and the loaded candles stay while another tab is in front; refreshes pause when hidden.
registerScreen({ id: "chart", component: ChartScreen, keepAlive: true, title: (params) => params.symbol ?? "" });
registerSlot("addMenu.assets", AssetsMenu, { order: 0 });
