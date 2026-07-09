# Strategie-Triage — Phase 3

> Erstellt: 2026-02-25
> Quellen: ~/Desktop/strategie-ideen/ (3 Ordner, 41 Strategien)
> Verfügbare Daten: 1h (AAPL, AMZN, JNJ, JPM, MSFT), 1d (+ GOOGL, KO, NVDA, PG, XOM), 5m (76 Symbole), XAUUSD (Dukascopy)

---

## Vielversprechend (12 Strategien)

| # | Name | Quelle | Format | Logik | Assets | Kompl. | Notizen |
|---|------|--------|--------|-------|--------|--------|---------|
| 1 | **Short-Term Reversal** | desenyon | Python (QC) | 5-Tage-Return Reversal: Long Bottom-20, Short Top-20 aus 500+ Aktien. Regime-Filter (SPY), Sektor-Limits. | US Equities (daily) | M | Akademisch belegt (Jegadeesh 1990). Sofort konvertierbar mit vorhandenen Daily-Daten. |
| 2 | **Trend Following CTA** | desenyon | Python (QC) | Multi-Timeframe Momentum über 13 ETFs (Equities, Bonds, FX, Commodities). Volatility-basiertes Position Sizing, adaptive Stops. | Multi-Asset (daily) | M | Bewährter CTA-Ansatz. Braucht ETF-Daten (Download nötig). Adaptierbar auf vorhandene Aktien. |
| 3 | **SPY Mean Reversion** | desenyon | Python (QC) | Ornstein-Uhlenbeck + Z-Scores + Volume Profile. Bollinger/RSI-basierte Extremwert-Erkennung. UVXY Hedge. | SPY/UVXY (minute) | M | Statistisch fundiert. Adaptierbar auf einzelne Aktien mit vorhandenen Daten. |
| 4 | **Sector Rotation** | desenyon | Python (QC) | 11 Sektor-ETFs rotieren nach Business Cycle + Relative Strength (1m/3m/6m Momentum). | Sektor-ETFs (daily) | M | Sektor-Trends sind persistent. Braucht Sektor-ETF-Daten. |
| 5 | **Gold Momentum** | desenyon | Python (QC) | Multi-TF Momentum (20/60/120d) auf GLD. MACD, RSI, BB Signale. Volatility-adjusted Sizing. TLT Risk-Off. | GLD/TLT (hourly) | M | XAUUSD-Daten vorhanden (Dukascopy). Sofort testbar. |
| 6 | **Simple Intraday Volatility Breakout** | discord (zenomtrader) | NT8 (C#) | RTH Open + 0.3×ATR(5d) als Breakout-Level. Long/Short bei Break. Stop @ RTH Open. | Futures/Equities (5m-1h) | S | Sehr einfach. Sofort konvertierbar. 1h-Daten reichen. |
| 7 | **Intraday Channel Breakout NQ** | discord (zenomtrader) | NT8 (C#) | N-Bar High/Low Breakout + 200 SMA Trendfilter + Gap-Filter + ATR Trailing Stop. | NQ/Equities (5min RTH) | M | Gut strukturiert mit mehreren Filtern. |
| 8 | **Post VIX Dip Buy** | discord (zenomtrader) | Pine v5 | VIX Spike >10% + SPX down >1% = Panik-Regime. Kaufe frühen Dip (RSI<40, unter VWAP). 2% TP, 0.3% SL. | SPX/ES (intraday) | M | Cleverer Regime-Filter. Braucht VIX-Daten. Seltene Setups (5-10/Jahr). |
| 9 | **Statistical Arbitrage Pairs** | desenyon | Python (QC) | Kointegrations-basiertes Pairs Trading (8 Paare: XOM/CVX, JPM/BAC, etc.). Kalman-Filter Hedge Ratios. | Equity Pairs (hourly) | M-L | Market-neutral. Braucht Paar-Daten (teilweise vorhanden). |
| 10 | **Risk Parity** | desenyon | Python (QC) | Equal Risk Contribution über 11 Assets. Leverage auf 10% Volatility Target. Korrelations-Regime-Detection. | Multi-Asset (daily) | L | Institutional. Braucht breite Asset-Daten. |
| 11 | **Cross-Asset Momentum** | desenyon | Python (QC) | Time-Series & Cross-Sectional Momentum über 27 Assets. Dynamic Risk Parity Weighting + Crash Protection. | 27 ETFs (daily) | L | Diversifiziert. Sehr datenintensiv. |
| 12 | **Multi-Factor Equity** | desenyon | Python (QC) | Value/Momentum/Quality/Low-Vol Faktoren (0.25/0.30/0.25/0.20). Sektor-neutrale Konstruktion. | 100+ Equities (daily) | L | Akademisch belegt. Braucht Fundamental-Daten. |

---

## Vielleicht (12 Strategien)

| # | Name | Quelle | Format | Logik | Assets | Kompl. | Notizen |
|---|------|--------|--------|-------|--------|--------|---------|
| 13 | **Daily Bias ORB Anti ICT** | discord (zenomtrader) | NT8 (C#) | Opening Range (9:30-10:00) Breakout + 200 EMA Trend + Gap Filter. | Equities (intraday) | M | Kein Take-Profit definiert — muss ergänzt werden. |
| 14 | **Volatility Trading** | desenyon | Python (QC) | Short Vol via SVXY/UVXY. VIX Term Structure + Risk Premium. SPY/TLT Hedge. | UVXY/SVXY (hourly) | M | VRP edge fades post-2020. Riskant. |
| 15 | **Factor Timing** | desenyon | Python (QC) | Dynamische Allokation über 5 Equity-Faktoren via 12 Faktor-ETFs. Faktor-Momentum + Macro. | 12 Faktor-ETFs | M | Smart Beta. Faktor-Rotation ist schwierig. |
| 16 | **Seasonal Patterns** | desenyon | Python (QC) | Kalender-Anomalien: Turn-of-Month, Holiday Effects, Quarterly Patterns über 17 ETFs. | 17 ETFs (daily) | M | Anomalien könnten weggehandelt sein. |
| 17 | **Sentiment Momentum** | desenyon | Python (QC) | Price Momentum (60d) + Sentiment Proxies (VXX, HYG, GLD, TLT). Options Flow Signale. | 25+ Stocks + Sentiment | L | Sentiment-Signale sind noisy. |
| 18 | **Tiingo Sentiment** | alexisbouley | Python (QC) | Daily Long Top-5 / Short Bottom-5 nach Keyword-Sentiment-Scoring. 50 Large Caps. | 50 Stocks + Tiingo | M | Primitive Sentiment-Methode. Nur in Krisen profitabel. |
| 19 | **Event-Driven Earnings** | desenyon | Python (QC) | Post-Earnings Announcement Drift (PEAD). 20-Tage Hold nach Earnings. | 15 Stocks + Earnings Dates | M-L | PEAD ist bekannt und crowded. |
| 20 | **Global Macro** | desenyon | Python (QC) | Top-Down Macro Allocation über 27 Assets. Growth/Inflation/Monetary Policy Regimes. | 27 Assets + Macro | L | Braucht starkes Macro-Forecasting. |
| 21 | **Carry Trade** | desenyon | Python (QC) | Yield-Differentials über FX, Bonds, Commodities. Dynamic Sizing nach Spread. | FX/Bonds/Commodities | M | Viable aber braucht spezifische Daten. |
| 22 | **Covered Call Wheel** | desenyon | Python (QC) | Systematisch Puts verkaufen (cash-secured), Calls verkaufen (covered). Options-Rollen bei <7 DTE. | Stocks + Options | M | Retail-freundlich aber braucht Options-Daten. |
| 23 | **ML Momentum** | desenyon | Python (QC) | Ensemble: 8 Features (Momentum, Vol, RSI, Earnings). Weighted Scoring. Top 25 Long, Bottom 10 Short. | Universe + Fundamentals | M | Interessant für ML-Integration (Phase 3 Prio 7). |
| 24 | **Treasury Yield Curve** | desenyon | Python (QC) | Curve Slope Trading, Inversion Signals, Duration Positioning über 5 Laufzeiten. | Treasury Yields | M | Nische. Braucht Yield-Daten. |

---

## Müll (17 Strategien)

| # | Name | Quelle | Grund |
|---|------|--------|-------|
| 25 | **Opening Range Breakout** | alexisbouley | ❌ Sharpe -0.4 out-of-sample. Extrem parameterabhängig (Sharpe 1.11 vs 2.69 bei verschiedenen `breakout_buffer` Werten). Overfitting bestätigt. |
| 26 | **Overnight Gap Mean Reversion** | alexisbouley | ❌ -62.4% Return, Sharpe -2.2. Bid-Ask Spread bei Market Open (9:31) zerstört jeden Edge. Reverse-Strategie ebenfalls negativ. |
| 27 | **US100 Intraday Scalper** | discord (zenomtrader) | ❌ Fixed-Point Stops (0.1 Pts) skalieren nicht. Midnight-Close-Referenz ist fragil. Zu simplistisch, kein Trendfilter. |
| 28 | **Adaptive Market Making** | desenyon | ❌ HFT — braucht Sub-Sekunden-Execution + Orderbuch-Daten. Nicht für Retail. |
| 29 | **High-Freq Stat Arb** | desenyon | ❌ Intraday Pairs auf Minute-Level. Braucht extrem niedrige Latenz. Nicht für Retail. |
| 30 | **Options Volatility Arb** | desenyon | ❌ Delta-Hedging alle 30 Min + Straddle-Execution. Braucht Options-Kette in Echtzeit. |
| 31 | **Convertible Arbitrage** | desenyon | ❌ Braucht Convertible-Bond-Daten + Delta-Rebalancing. Institutionell. |
| 32 | **Dispersion Trading** | desenyon | ❌ Short Index-Vol, Long Stock-Vol. Braucht IV Surfaces. Institutionell. |
| 33 | **Variance Swap Replication** | desenyon | ❌ Synthetische Varianz-Swaps via Options + dynamisches Hedging. Institutionell. |
| 34 | **Merger Arbitrage** | desenyon | ❌ Braucht Corporate-Action-Daten + Deal-Tracking in Echtzeit. |
| 35 | **Relative Value Credit** | desenyon | ❌ Credit-Spread-Dynamics über IG/HY/EM. Braucht Credit-Index-Daten. |
| 36 | **Dollar Neutral** | desenyon | ❌ Market-neutral L/S mit Beta/Sector Constraints. Braucht Fundamental-Daten + kurze Aktien (Short-Selling). |
| 37 | **Alternative Data** | desenyon | ❌ Braucht Social-Media-Sentiment, Earnings Surprises, Analyst Revisions. Daten nicht verfügbar. |
| 38 | **Multi-Asset Vol Target** | desenyon | ❌ Konzeptionell gut (10-15% Vol Target), aber braucht 11 diversifizierte Assets + Korrelationen. Zu datenhungrig für aktuellen Stand. |
| 39 | **Tail Risk Hedging** | desenyon | ❌ Ständig OTM Puts kaufen = Drag auf Returns. Nur sinnvoll als Overlay auf bestehendes Portfolio. |
| 40 | **Crypto Momentum** | desenyon | ❌ BTC/ETH Momentum. Andere Asset-Klasse, keine Daten, keine Exchange-Anbindung. |
| 41 | **Intraday Momentum** | desenyon | ❌ ORB + VWAP auf SPY/QQQ/IWM auf Minute-Level. Braucht Minute-Daten die wir nicht in der Qualität haben. |

---

## Top 5 für Konvertierung

Basierend auf: Daten-Verfügbarkeit, Logik-Klarheit, Konvertierbarkeit nach vectorbt.

| Rang | Strategie | Warum |
|------|-----------|-------|
| **1** | Short-Term Reversal | Akademisch belegt, einfache Logik, funktioniert mit Daily-Daten (9 Symbole vorhanden) |
| **2** | Simple Intraday Volatility Breakout | Sehr einfach, klare Regeln, 1h-Daten reichen, sofort testbar |
| **3** | SPY Mean Reversion (adaptiert) | RSI/BB Mean Reversion auf Einzelaktien. Vorhandene 1h-Daten. Statistisch fundiert. |
| **4** | Gold Momentum (adaptiert) | XAUUSD-Daten vorhanden (Dukascopy). Multi-TF Momentum mit MACD/RSI. |
| **5** | Trend Following CTA (adaptiert) | Multi-TF Momentum auf Daily-Aktien. Adaptiert von 13 ETFs auf verfügbare Symbole. |

---

## Daten-Bedarf für Top-Strategien

| Strategie | Vorhandene Daten | Fehlende Daten |
|-----------|-----------------|----------------|
| Short-Term Reversal | ✅ 9 Symbole Daily (2021-2025) | ⚠️ Nur 9 Symbole statt 500+. Reicht für Proof-of-Concept. |
| Volatility Breakout | ✅ 5 Symbole 1h (2023-2026) | ✅ Ausreichend |
| Mean Reversion | ✅ 5 Symbole 1h (2023-2026) | ✅ Ausreichend |
| Gold Momentum | ✅ XAUUSD 1h (Dukascopy) | ⚠️ Timestamps prüfen (sehen fehlerhaft aus) |
| Trend Following | ✅ 9 Symbole Daily (2021-2025) | ⚠️ Diversifikation eingeschränkt (nur US Equities) |
