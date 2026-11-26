import type { DeskTab } from "../panels";
import type { DeskSummary } from "../types";

/** What every tab of a desk receives. */
export interface TabProps {
  desk: DeskSummary;
  /** Opens the Company panel with this asset (a click on a ticker of another panel). */
  onOpenCompany?: (symbol: string) => void;
  /** The asset another panel asked Company to show; it may not be one of the desk's. */
  focusSymbol?: string | null;
  /** The panel took `focusSymbol`: it is cleared, so pressing the same ticker again opens it again. */
  onFocusUsed?: () => void;
  /** Adds a panel to the desk (if it is not there) and goes to it: "Open detail" of the Strategies panel uses it. */
  openPanel?: (panel: DeskTab) => void;
}
