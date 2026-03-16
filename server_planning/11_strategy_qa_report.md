# Strategy QA Report

Ausfuehrung: Source-URL-Reparatur, Plot/Indicator-Integration, Playwright-TradeChart-QA.

## QA-Ergebnis pro Strategie

| Strategie | source_url repariert | Hinzugefuegte Plots/Indikatoren | Visueller Trade-Viewer Test |
|---|---|---|---|
| `channel_breakout` | Ja (`Discord: zenomtrader`) | `indicators_dict` vorhanden (EMA_200, Upper_Channel, Lower_Channel, ATR, sl_price, trailing_stop_at_exit); zusaetzlich Marker `arrowUp` (Entry) & `arrowDown` (Exit) in `drawings`. | Nein - Chart-Canvas laedt, aber Playwright meldet `6` Page-Errors (`Value is null`); Screenshot: `/home/ubuntu/Desktop/server_planning/qa_errors/error_channel_breakout.png` |
| `post_vix_dip_buy` | Ja (`Discord: zenomtrader`) | `indicators_dict` vorhanden (VIX, VIX_at_exit, RSI, RSI_at_exit, SMA, SMA_at_exit); zusaetzlich Marker `arrowUp` (Entry) & `arrowDown` (Exit) in `drawings`. | Nein - Chart-Canvas laedt, aber Playwright meldet `9` Page-Errors (`Value is null`); Screenshot: `/home/ubuntu/Desktop/server_planning/qa_errors/error_post_vix_dip_buy.png` |
| `volatility_breakout` | Ja (`Discord: zenomtrader`) | `indicators_dict` vorhanden (Session_Open, Session_Open_at_exit, ATR, Upper_Band, Lower_Band, sl_price); zusaetzlich Marker `arrowUp` (Entry) & `arrowDown` (Exit) in `drawings`. | Nein - Chart-Canvas laedt, aber Playwright meldet `6` Page-Errors (`Value is null`); Screenshot: `/home/ubuntu/Desktop/server_planning/qa_errors/error_volatility_breakout.png` |
| `mean_reversion_rsi_bb` | Ja (`https://github.com/alexisbouley2/quantconnect/tree/main/strategies/overnight_gap_mean_reverse`) | `indicators_dict` vorhanden (RSI, RSI_at_exit, BB_Mid, BB_Mid_at_exit, BB_Upper, BB_Lower); zusaetzlich Marker `arrowUp` (Entry) & `arrowDown` (Exit) in `drawings`. | Nein - Chart-Canvas laedt, aber Playwright meldet `9` Page-Errors (`Value is null`); Screenshot: `/home/ubuntu/Desktop/server_planning/qa_errors/error_mean_reversion_rsi_bb.png` |
| `cross_asset_momentum` | Ja (`https://github.com/desenyon/quantconnect-strategies/blob/main/cross_asset_momentum.py`) | `indicators_dict` vorhanden (Composite_Momentum, Composite_Momentum_at_exit, Vol_Adjusted_Momentum, Rolling_Drawdown, Rolling_Drawdown_at_exit, ATR); zusaetzlich Marker `arrowUp` (Entry) & `arrowDown` (Exit) in `drawings`. | Nein - Chart-Canvas laedt, aber Playwright meldet `6` Page-Errors (`Value is null`); Screenshot: `/home/ubuntu/Desktop/server_planning/qa_errors/error_cross_asset_momentum.png` |
| `risk_parity` | Ja (`https://github.com/desenyon/quantconnect-strategies/blob/main/risk_parity_strategy.py`) | `indicators_dict` vorhanden (Rolling_Vol, Rolling_Vol_at_exit, Vol_Median, Trend_SMA, Trend_SMA_at_exit, Calm_Regime); zusaetzlich Marker `arrowUp` (Entry) & `arrowDown` (Exit) in `drawings`. | Nein - Chart-Canvas laedt, aber Playwright meldet `6` Page-Errors (`Value is null`); Screenshot: `/home/ubuntu/Desktop/server_planning/qa_errors/error_risk_parity.png` |
| `sector_rotation` | Ja (`https://github.com/desenyon/quantconnect-strategies/blob/main/sector_rotation_strategy.py`) | `indicators_dict` vorhanden (Momentum_63, Momentum_63_at_exit, Momentum_21, SMA_200, Short_Weakness, Short_Weakness_at_exit); zusaetzlich Marker `arrowUp` (Entry) & `arrowDown` (Exit) in `drawings`. | Nein - Chart-Canvas laedt, aber Playwright meldet `6` Page-Errors (`Value is null`); Screenshot: `/home/ubuntu/Desktop/server_planning/qa_errors/error_sector_rotation.png` |
| `short_term_reversal` | Ja (`https://github.com/desenyon/quantconnect-strategies/blob/main/short_term_reversal_strategy.py`) | `indicators_dict` vorhanden (Return_5D, Return_5D_at_exit, RSI, ATR, sl_price, bars_held_at_exit); zusaetzlich Marker `arrowUp` (Entry) & `arrowDown` (Exit) in `drawings`. | Nein - Chart-Canvas laedt, aber Playwright meldet `9` Page-Errors (`Value is null`); Screenshot: `/home/ubuntu/Desktop/server_planning/qa_errors/error_short_term_reversal.png` |
| `stat_arb_pairs` | Ja (`https://github.com/desenyon/quantconnect-strategies/blob/main/statistical_arbitrage_pairs.py`) | `indicators_dict` vorhanden (ZScore, ZScore_at_exit, Rolling_Mean, Rolling_Std, ATR, sl_price); zusaetzlich Marker `arrowUp` (Entry) & `arrowDown` (Exit) in `drawings`. | Nein - Chart-Canvas laedt, aber Playwright meldet `6` Page-Errors (`Value is null`); Screenshot: `/home/ubuntu/Desktop/server_planning/qa_errors/error_stat_arb_pairs.png` |
| `trend_following` | Ja (`https://github.com/desenyon/quantconnect-strategies/blob/main/trend_following_cta.py`) | `indicators_dict` vorhanden (SMA_Fast, SMA_Mid, SMA_Slow, SMA_Fast_at_exit, SMA_Mid_at_exit, ATR); zusaetzlich Marker `arrowUp` (Entry) & `arrowDown` (Exit) in `drawings`. | Nein - Chart-Canvas laedt, aber Playwright meldet `6` Page-Errors (`Value is null`); Screenshot: `/home/ubuntu/Desktop/server_planning/qa_errors/error_trend_following.png` |
| `gold_momentum` | Ja (`https://github.com/desenyon/quantconnect-strategies/blob/main/gold_momentum_strategy.py`) | `indicators_dict` vorhanden (MACD_Line, MACD_Line_at_exit, MACD_Signal, MACD_Signal_at_exit, RSI, RSI_at_exit); zusaetzlich Marker `arrowUp` (Entry) & `arrowDown` (Exit) in `drawings`. | Nein - Chart-Canvas laedt, aber Playwright meldet `13` Page-Errors (`Value is null`); Screenshot: `/home/ubuntu/Desktop/server_planning/qa_errors/error_gold_momentum.png` |

## Backtest-Laeufe fuer die visuelle QA

| Strategie | Backtest-ID | Trades |
|---|---|---:|
| `channel_breakout` | `vbt-channel_breakout-20260316-172656` | 285 |
| `post_vix_dip_buy` | `vbt-post_vix_dip_buy-20260316-172702` | 388 |
| `volatility_breakout` | `vbt-volatility_breakout-20260316-172708` | 38 |
| `mean_reversion_rsi_bb` | `vbt-mean_reversion_rsi_bb-20260316-172714` | 156 |
| `cross_asset_momentum` | `vbt-cross_asset_momentum-20260316-172720` | 85 |
| `risk_parity` | `vbt-risk_parity-20260316-172727` | 73 |
| `sector_rotation` | `vbt-sector_rotation-20260316-172733` | 199 |
| `short_term_reversal` | `vbt-short_term_reversal-20260316-172814` | 91 |
| `stat_arb_pairs` | `vbt-stat_arb_pairs-20260316-172743` | 142 |
| `trend_following` | `vbt-trend_following-20260316-172749` | 61 |
| `gold_momentum` | `vbt-gold_momentum-20260316-172755` | 253 |

## Hinweis

- Die Trade-Charts wurden fuer alle Strategien geoeffnet (erster Trade aus Tabelle).
- Alle Strategien zeigen reproduzierbare Frontend-Page-Errors (`Value is null`) im TradeChart-Kontext.
- Fehler-Screenshots wurden gem. Vorgabe unter `qa_errors/` abgelegt.
