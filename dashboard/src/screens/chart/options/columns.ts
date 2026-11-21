/** The columns of each side, in the order they are drawn. Gamma and vega are in no set: they are for the agent. */
export type ColumnId = "last" | "probItm" | "intrinsic" | "openInterest" | "volume" | "theta" | "bid" | "ask" | "delta" | "iv";

export type ColumnSetId = "1" | "2";

export interface ColumnSet {
  calls: readonly ColumnId[];
  puts: readonly ColumnId[];
}

export const COLUMN_SETS: Record<ColumnSetId, ColumnSet> = {
  "1": {
    calls: ["last", "probItm", "intrinsic", "openInterest", "volume", "theta", "bid", "ask"],
    puts: ["bid", "ask", "last", "probItm", "intrinsic", "openInterest", "volume", "theta"],
  },
  "2": {
    calls: ["last", "probItm", "intrinsic", "openInterest", "volume", "theta", "delta", "iv", "bid", "ask"],
    puts: ["bid", "ask", "delta", "iv", "last", "probItm", "intrinsic", "openInterest", "volume", "theta"],
  },
};

export const DEFAULT_COLUMN_SET: ColumnSetId = "1";

/** Catalog key of the title of a column, and of its tooltip when Hokma calculates it. */
export const COLUMN_KEY: Record<ColumnId, string> = {
  last: "options.col.last",
  probItm: "options.col.probItm",
  intrinsic: "options.col.intrinsic",
  openInterest: "options.col.openInterest",
  volume: "options.col.volume",
  theta: "options.col.theta",
  bid: "options.col.bid",
  ask: "options.col.ask",
  delta: "options.col.delta",
  iv: "options.col.iv",
};
export const COLUMN_TITLE: Partial<Record<ColumnId, string>> = {
  probItm: "options.col.probItm.title",
  intrinsic: "options.col.intrinsic.title",
  iv: "options.col.iv.title",
};
