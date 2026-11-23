# Indicators module (SVC-indicators)

Every function of the indicators module is registered as `indicators.<name>` in `hokma.quant.registry` and is called by agents through `quant_call`.
Agents never import a library: only `backend/hokma/quant/indicators/_lib.py` does. Bars are daily bars (a table with open, high, low, close, volume).
The recipe of every indicator is written in this repository from its published definition; the citation of the sources of the
data and the licenses lives only in `.notices/THIRD_PARTY.txt`.

## How to read the table

- **Backing**: `library` means the function calls a technical analysis function through `_lib.py` (parity with it is tested to 1e-8); `own` means the formula is
  written in this module; a mix says which part is which.
- **Measured causal lag**: the lag in bars of the level the indicator follows, measured on a unit ramp (a series that rises one unit per bar): the median over the
  last 50 bars of the close minus the output. A moving average of window N has lag (N - 1) / 2; the weighted average has (N - 1) / 3. An oscillator or a ratio has no
  lag in this sense (shown as n/a). The tests recompute every number of this column.
- **Lines**: `line` selects the windows. SWO and SWC use the daily windows of the table, DT halves each window, LP doubles it; an explicit window always wins. The lag grows with the window.
- **Causal**: every value at bar t uses only bars up to t, except the two filters marked research only.

## Functions

| Function | Pieces | Backing | Causal | Measured causal lag (bars) |
|---|---|---|---|---|
| `indicators.accumulation_distribution` | FT-077 | library | causal | n/a |
| `indicators.advance_decline` | FT-077 | own | causal | n/a |
| `indicators.adx` | FT-078 | library, own DI lines | causal | n/a |
| `indicators.aroon` | FT-078 | library, own oscillator | causal | n/a |
| `indicators.atr` | FT-080 | library | causal | n/a |
| `indicators.awesome_oscillator` | FT-078 | library | causal | n/a |
| `indicators.balance_of_power` | FT-078 | library | causal | n/a |
| `indicators.bollinger` | FT-080 | library, own composition | causal | 9.5000 (middle band, window 20) |
| `indicators.butterworth` | ECT-016 | own over library filter design | causal | 2.1763 (cutoff period 10, order 2) |
| `indicators.catalog_value` | TA-74 | own (delegates to the indicators above) | causal | n/a |
| `indicators.cci` | FT-078 | library | causal | n/a |
| `indicators.chaikin_money_flow` | FT-077 | library | causal | n/a |
| `indicators.chaikin_oscillator` | FT-077 | library | causal | n/a |
| `indicators.chande_momentum` | FT-078 | library | causal | n/a |
| `indicators.chandelier_exit` | FT-080 | library | causal | n/a |
| `indicators.choppiness` | FT-078 | library | causal | n/a |
| `indicators.cycle_check` | ECT-016 | own | causal | n/a |
| `indicators.dominant_cycle` | ECT-016 | own | causal | n/a |
| `indicators.donchian` | FT-080 | library | causal | 10.5000 (middle channel, window 20) |
| `indicators.ease_of_movement` | FT-077 | library | causal | n/a |
| `indicators.ema` | FT-079 | library | causal | 10.0000 (window 21) |
| `indicators.fibonacci` | FT-079 | library on rolling extremes | causal | n/a |
| `indicators.force_index` | FT-078 | library | causal | n/a |
| `indicators.hodrick_prescott` | ECT-016 | own over library banded solver | research only, two sided: blocked for live runs, repaints | n/a |
| `indicators.hull` | FT-079 | library | causal | 0.6667 (window 20) |
| `indicators.ichimoku` | FT-078 | library, own lagging close | causal | 4.0000 conversion line, 12.5000 base line (windows 9 and 26) |
| `indicators.kama` | FT-079 | library | causal | 1.2500 (window 10, fast 2, slow 30) |
| `indicators.keltner` | FT-080 | library | causal | 9.5000 (middle line, window 20) |
| `indicators.macd` | FT-078 | library, own histogram | causal | n/a |
| `indicators.mcclellan` | FT-077 | own, EMA from library | causal | n/a |
| `indicators.measure_lag` | ECT-016 | own | causal | n/a |
| `indicators.mfi` | FT-078 | library | causal | n/a |
| `indicators.negative_volume_index` | FT-077 | library | causal | n/a |
| `indicators.new_highs_lows` | FT-077 | library | causal | n/a |
| `indicators.obv` | FT-077 | library | causal | n/a |
| `indicators.parabolic_sar` | FT-079 | library, own trend flag | causal | n/a |
| `indicators.percent_above_average` | FT-077 | own | causal | n/a |
| `indicators.pivot_points` | FT-079 | library | causal | n/a |
| `indicators.positive_volume_index` | FT-077 | library | causal | n/a |
| `indicators.ppo` | FT-078 | library | causal | n/a |
| `indicators.price_volume_trend` | FT-077 | library | causal | n/a |
| `indicators.roc` | FT-078 | library | causal | n/a |
| `indicators.rsi` | FT-078 | library | causal | n/a |
| `indicators.savgol_causal` | ECT-016 | own over library coefficients | causal | 0.0000 (window 11, order 2) |
| `indicators.savgol_centered` | ECT-016 | library | research only, two sided: blocked for live runs, repaints | n/a |
| `indicators.screens.distribution_days` | CTS-028 | own, EMA from library | causal | n/a |
| `indicators.screens.earnings_reaction` | CTS-002 | own | causal | n/a |
| `indicators.screens.episodic_pivot` | CTS-017 | own | causal | n/a |
| `indicators.screens.momentum_burst` | CTS-004 | own | causal | n/a |
| `indicators.screens.pead` | CTS-001 | own | causal | n/a |
| `indicators.screens.vcp` | CTS-020 | own | causal | n/a |
| `indicators.screens.vcp_forward_outcome` | CTS-020 | own | causal | n/a |
| `indicators.screens.weekly_structure` | CTS-005 | own | causal | n/a |
| `indicators.session_vwap` | FT-079 | own | causal | n/a |
| `indicators.sma` | FT-079 | library | causal | 24.5000 (window 50) |
| `indicators.stochastic` | FT-078 | library | causal | n/a |
| `indicators.stochastic_rsi` | FT-078 | library | causal | n/a |
| `indicators.supertrend` | FT-080 | library | causal | n/a |
| `indicators.support_resistance` | FT-079 | library | causal | n/a |
| `indicators.trin` | FT-077 | library | causal | n/a |
| `indicators.true_range` | FT-080 | library | causal | n/a |
| `indicators.true_strength_index` | FT-078 | library | causal | n/a |
| `indicators.ultimate_oscillator` | FT-078 | library | causal | n/a |
| `indicators.volatility_cone` | FT-080 | own | causal | n/a |
| `indicators.vortex` | FT-078 | library | causal | n/a |
| `indicators.vwma` | FT-079 | own | causal | 9.5000 (window 20, constant volume) |
| `indicators.wavelet_denoise` | ECT-016 | own | causal | 2.5690 (window 64, level 3) |
| `indicators.williams_r` | FT-078 | library | causal | n/a |
| `indicators.wma` | FT-079 | library | causal | 6.3333 (window 20) |

## Notes

- Range based volatility estimators (Yang-Zhang, Rogers-Satchell) are written once, in the statistics module; the indicators module exposes them under the same names as aliases of those functions.
- The screens read only closed bars: a daily bar is closed once 16:00 New York of its date has passed at `as_of`, a weekly bar once its Friday has. Their entry rule is the next open plus one tick; the stop is checked first when one bar touches both.
- The score weights of the earnings reaction and of the drift screen are not validated: results say `weights_unvalidated` until a backtest sets them.
- A filter marked research only needs `mode="research"`; with the default `mode="live"` it returns no value and a warning.
- A filter shows the lag it adds: report `indicators.measure_lag` next to any filtered value, and test a cycle that a filter seems to show with `indicators.cycle_check`.
