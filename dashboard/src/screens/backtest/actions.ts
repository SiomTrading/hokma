/**
 * What each `action` code of SPEC-032 section 8 asks of the creator, in English: this is the text the owner copies and pastes
 * in the chat of the strategy creator, so it does not change with the language of the screen. A code that is not here is
 * copied with the text the server sent.
 */
export const ACTION_TEXT_EN: Record<string, string> = {
  answer_question: "(For the owner) Answer the question on the screen.",
  use_point_in_time_data: "Use data without later corrections or drop the entry that depends on them.",
  include_delisted: "Include the names that disappeared or reduce the universe to instruments that existed the whole period.",
  apply_corporate_actions: "Use the continuous series of the service or a series adjusted for splits and dividends.",
  fix_lookahead: "Some data reads the future: review indicators and labels; run the walk-forward to measure the leak.",
  clean_data: "Exclude invalid bars and repeat the run.",
  count_trials: "Run the trials through the service so the ledger counts them.",
  tune_in_sample: "Choose parameters with walk-forward, never with the test period.",
  reserve_holdout: "Keep a final period unseen and use it only once.",
  lengthen_history: "Lengthen the history or use a time frame with more trades.",
  charge_costs: "Run with the house commission table and slippage.",
  reduce_turnover: "Trade less or with a wider target: the costs eat too much of the edge.",
  model_impact: "Add an impact model or reduce the size.",
  charge_financing: "Include the cost of financing or borrowing for the shorts.",
  use_house_fill: "Use the house fill: next open plus 1 tick, stop first, gap at the open.",
  run_walk_forward: "Run walk-forward (rolling or anchored) or combinatorial validation.",
  deflate_sharpe: "Compute the deflated Sharpe with the count of the ledger.",
  beat_chance: "The Sharpe does not beat luck for this many trials: fewer variants or more history.",
  raise_dsr: "Fewer parameters, fewer variants tried or more history.",
  reduce_overfit: "Fewer parameters or shorter ranges; more history.",
  seek_plateau: "The strategy is fragile: use the center of the widest plateau or simplify the rule.",
  widen_range: "Widen the range of the parameters on the border beyond the best value.",
  regime_filter: "Add a regime filter or switch the strategy off in the regimes where it loses.",
  audit_for_bug: "A Sharpe this high is usually a bug: check costs, fills and data from the future.",
  diversify_trades: "One trade makes most of the profit: check whether the result depends on a single event.",
  stabilize_oos: "Little of the result holds out of sample: fewer parameters or simpler rules.",
  split_costs: "Report the four costs separately (commission, slippage, swap, impact).",
  report_drawdown: "Compute the maximum drawdown and its duration with the service.",
};
