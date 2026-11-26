import { registerScreen } from "@/shell/registry";
import { BacktestScreen } from "./BacktestScreen";

// The screen meta (title, icon) is built in: see BUILTIN_SCREENS. With {strategy, version} in the tab params it opens that
// version (the strategy bank of SPEC-030 opens it that way); without them it shows the selector.
registerScreen({
  id: "backtest",
  component: BacktestScreen,
  title: (params, t) => (params.strategy ? `${t("screen.backtest")}: ${params.strategy}${params.version ? ` v${params.version}` : ""}` : t("screen.backtest")),
});
