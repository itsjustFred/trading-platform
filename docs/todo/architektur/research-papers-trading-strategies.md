# Trading-Strategie Papers & Ressourcen — Research-Report
**Erstellt:** 2026-06-22 (für Discord-Report am 23.06. ~10:00 MESZ)
**Quelle:** Web-Search (arXiv, SSRN, ScienceDirect, QuantInsti, Concretum Group, NYU Stern, etc.)

---

## 🎯 Direkt relevant für die Trading-Plattform

### 1. Walk-Forward Optimization (WFO) — Dein TODO-Punkt
| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **A novel approach to trading strategy parameter optimization using double out-of-sample data and walk-forward techniques** | 2026 | Sehr hoch — direkter WFO-Ansatz für EMA-Strategien auf Bitcoin (1m–60m), getestet mit Robust Sharpe Ratio, 81 Window-Kombinationen | [arXiv:2602.10785](https://arxiv.org/abs/2602.10785) |
| **Validating a Hybrid Trading Strategy with Walk-Forward Optimization** | 2025 | Hoch — LSTM-ARIMA + technische Indikatoren (RSI, MACD, BB), WFO auf ETH, OOS-Sharpe 5.28, Return 38.89%, DD 23.58% | [InsiderFinance](https://wire.insiderfinance.io/validating-a-hybrid-trading-strategy-with-walk-forward-optimization-b109a400f30a) |
| **The Efficacy of Algorithmic Trading with Walk-Forward Optimization: SOL/USD** | 2025 | Hoch — WFO mit Keras-Tuner auf SOL/USD, implementiert in Python (`SOL_WFO_POC.ipynb`), GitHub verfügbar | [InsiderFinance](https://wire.insiderfinance.io/the-efficacy-of-walk-forward-optimization-in-algorithmic-trading-eth-usd-905207f6b0ef) |
| **Walk Forward Optimization: How It Works, Its Limitations, and Backtesting Implementation** | 2024 | Hoch — QuantInsti Guide, Python-Implementierung, Theorie + Praxis | [QuantInsti](https://blog.quantinsti.com/walk-forward-optimization-introduction) |
| **Walk Forward Optimization with XGBoost for Stock Price Prediction** | 2025 | Hoch — WFO + XGBoost für Aktien, Python-Code, Konzept-Drift behandelt | [QuantInsti](https://blog.quantinsti.com/walk-forward-optimization-python-xgboost-stock-prediction) |
| **Dennis Meyers Working Papers** | 2024/2025 | Hoch — Spezialisiert auf WFO für Futures/FX/Stocks, Repeated Median Velocity, Fixed Memory Polynomial, viele konkrete Beispiele (DIA 5m, CL 5m, QQQ daily) | [meyersanalytics.com](https://meyersanalytics.com/papers.html) |

### 2. Intraday Momentum & Mean Reversion (passend zu EURUSD H1, SP500/US100)
| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **Beat the Market: An Effective Intraday Momentum Strategy for SPY** | 2024 | Sehr hoch — Einfache Momentum-Strategie für SPY, 19.6% annualisiert, Sharpe 1.33, 2007–2024, inkl. Trailing-Stops, Slippage/Commissions berücksichtigt | [SSRN 4824172](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4824172) / [PDF](https://concretumgroup.com/wp-content/uploads/2026/02/Beat-the-Market.pdf) |
| **Momentum Exhaustion and Fair Value Reversion: ADX-conditioned VWAP Strategy in FX** | 2026 | Sehr hoch — Direkt für EURUSD FX: kombiniert VWAP, ADX, Momentum-Erschöpfung, Mean-Reversion. Backtest auf XAU/USD 15m geplant | [SSRN 6454659](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6454659) |
| **A Regime-Filtered Intraday Trading Framework for Gold (VWAP + EMA)** | 2026 | Hoch — VWAP + 200/50 EMA als Regime-Filter, 45.3% Win-Rate, Sharpe 3.99, DD 5.1% auf XAU/USD 15m | [SSRN 6650958](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6650958) |
| **QuanTip: Improving Performance with Fast Alphas — Tactical Overlay for Intraday Trend Trading** | 2026 | Hoch — Kurzfristige Mean-Reversion-Signale (5m SPY, 2007–2026) als Execution-Overlay für Trend-Following. Verbessert net CAGR + Sharpe nach Kosten | [Concretum Group](https://concretumgroup.com/quantip-improving-performance-with-fast-alphas-a-tactical-overlay-for-intraday-trend-trading/) / [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6391638) |
| **Intraday momentum and reversal in Chinese stock market** | 2019 | Mittel — Paper zeigt Momentum + Reversal-Effekte innerhalb von Halbstunden-Perioden. Nützlich für das Verständnis von Intraday-Patterns | [ScienceDirect](https://www.sciencedirect.com/science/article/pii/S1544612318307414) |

### 3. Risk Parity & Portfolio Optimization
| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **Hierarchical Risk Parity: Efficient Implementation and Real World Analysis** | 2025 | Sehr hoch — Effiziente HRP-Implementierung, getestet auf S&P 500 (2005–2023). HRP hat niedrigere Volatilität (~1%) als 1/N, aber 1/N schlägt HRP bei risk-adjusted returns. Wichtig für deinen bestehenden `optimizer.py` | [ScienceDirect](https://www.sciencedirect.com/science/article/pii/S0167739X25000391) |
| **An Elementary Introduction to Risk Parity** | 2022 | Hoch — Klare theoretische Herleitung von Risk Parity, Lineare Algebra + Summationsnotation. Gut zum Verstehen/Erklären | [SSRN 4250812](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4250812) |
| **Portfolio Optimization: A Comparative Study (MVP vs HRP vs Autoencoder)** | 2023 | Hoch — Vergleicht Mean-Variance, HRP und Autoencoder-Portfolios auf NSE India (2018–2022). MVP beste OOS-risk-adjusted, Autoencoder beste absolute Returns | [arXiv:2307.05048](https://arxiv.org/pdf/2307.05048v1) |
| **Diversified Risk Parity Strategies for Equity Portfolio Selection** | 2012 | Mittel — Konzentrierte Risk-Parity-Variante, die systematische Factor-Tilts besser ausnutzt als Standard-RP | [SSRN / ResearchGate](https://www.researchgate.net/publication/255726164_Diversified_Risk_Parity_Strategies_for_Equity_Portfolio_Selection) |
| **The Impact of Volatility Targeting** (Harvey) | 2018 | Hoch — Klassiker: Volatility Scaling verbessert Sharpe und reduziert Drawdowns signifikant. Grundlage für deinen `nextvol` Ansatz | [Duke PDF](https://people.duke.edu/~charvey/Research/Published_Papers/P135_The_impact_of.pdf) |
| **Volatility Targeting Is Trendy: How Trend Following Explains Alpha** | 2024 | Hoch — Zeigt, dass Volatility Targeting hauptsächlich via Trend-Following-Exposure funktioniert (Leverage-Effekt). Wichtig für das Verständnis von VT | [SSRN 4773781](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4773781) |

### 4. Deep Reinforcement Learning (DRL) für Trading
| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **Deep Reinforcement Learning for Automated Stock Trading: An Ensemble Strategy** | 2025 | Sehr hoch — Ensemble aus PPO, A2C, DDPG. Getestet auf 30 Dow Jones-Aktien. Sharpe-Ratio verbessert gegenüber Einzel-Algorithmen. Open-Source auf GitHub (FinRL) | [arXiv:2511.12120](https://arxiv.org/abs/2511.12120) |
| **Deep Reinforcement Learning for Trading** (Oxford) | 2019 | Sehr hoch — DQN/DRL auf 50 Futures-Kontrakte (2011–2019), übertrifft Time-Series-Momentum-Baselines. Volatility Scaling integriert | [arXiv:1911.10107](https://arxiv.org/abs/1911.10107) |
| **Algorithmic Trading by Reinforcement Learning in a Collaborative Manner** | 2025 | Hoch — DQN + GRU + TimesNet für Trading. Neueste Architekturen für sequenzielle Zeitreihen | [ScienceDirect](https://www.sciencedirect.com/science/article/pii/S1568494626006162) |
| **Reinforcement Learning Framework for Quantitative Trading** | 2024 | Mittel — Fokus auf technische Indikatoren als State-Input für RL-Agenten, Normalisierung, Hyperparameter-Tuning | [arXiv:2411.07585](https://arxiv.org/html/2411.07585v1) |
| **FinRL (AI4Finance Foundation)** | laufend | Sehr hoch — Open-Source Framework für Financial RL. Marktumgebungen, DRL-Agenten, Meta-Data (FinRL-Meta). Praktische Basis für eigene DRL-Experimente | [GitHub](https://github.com/AI4Finance-Foundation/FinRL) |

### 5. Meta-Learning & Ensemble-Methoden für Portfolio-Selektion
| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **Meta-Learning the Optimal Mixture of Strategies for Online Portfolio Selection** | 2025 | Sehr hoch — Clustering von historisch guten Strategien + Meta-Learning für schnelle Adaptation. Gut für deinen Multi-Strategy-Ansatz | [arXiv:2505.03659](https://arxiv.org/abs/2505.03659) |
| **Online Quantitative Trading Strategies** (NYU Stern) | 2025 | Hoch — Vergleicht CWMR, FTRL, PAMR, Pattern-Matching + Meta-Learning Ensembles (AA, FU, Newton Updates). Praktische Implementierungsdetails | [NYU Stern PDF](https://www.stern.nyu.edu/sites/default/files/2025-05/Glucksman_Lahanis.pdf) |
| **Revisiting Ensemble Methods for Stock Trading and Crypto** | 2025 | Hoch — GPU-parallele Simulationen (1746x Speedup), Ensemble-RL mit reduziertem Max-DD (+4.17% DD-Reduktion, +0.21 Sharpe) | [arXiv:2501.10709](https://arxiv.org/html/2501.10709v1) |

### 6. Volatility Forecasting & Volatility-Timing
| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **Volatility Forecasting and Volatility-Timing Strategies: A Machine Learning Approach** | 2025 | Sehr hoch — ML-Modelle (hochdimensional) schlagen Standard-GARCH für Volatility-Prognose. Volatility-Timing-Portfolios outperformen Markt besonders in volatilen Phasen | [ScienceDirect](https://www.sciencedirect.com/science/article/pii/S0275531924005166) |
| **Conditional Volatility Targeting Strategy Considering Jump Effects (ESG)** | 2024 | Mittel — ARMA-GARCH Jump-Modell für Volatility Targeting. Nützlich für fortgeschrittene Vol-Modelle | [ScienceDirect](https://www.sciencedirect.com/science/article/pii/S0927538X24002774) |

### 7. Market Microstructure & Order Flow (für Footprint-Charts / cTrader Integration)
| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **Order Flow Trading: 7 Proven Strategies for 2026** | 2026 | Hoch — Umfassender Guide zu Footprint-Charts, Delta, Imbalance, Absorption. Praktische Setups für Scalping/Intraday | [Kalena AI](https://blog.kalena.ai/order-flow-trading-the-complete-guide-to-reading-market-microstructure-and-trading-with-institutional-grade-dom-analysis-in-2026) |
| **Footprint Charts: Real-Time Order Flow Guide** | 2026 | Hoch — Detaillierte Erklärung von Footprint-Anatomie, Signalen (Absorption, Exhaustion, Imbalance) und Integration mit Volume Profile + VWAP | [AlgoStorm](https://algostorm.com/footprint-charts) |
| **Order Flow Analysis: Footprint Charts, Delta Divergence, Imbalance Detection** | 2026 | Mittel — Praktischer Leitfaden für Footprint-Setups, Risikomanagement, Plattformen | [Finowings](https://www.finowings.com/Trading/order-flow-analysis-footprint-delta) |
| **Market Microstructure and Order Flow Analysis** | — | Mittel — Überblick über Liquiditätsdynamik, Block-Trades, Footprint-Charts, Tools (Bookmap, Sierra Chart, Quantower) | [Proparison](https://www.proparison.com/strategy/market-microstructure-and-order-flow-analysis) |
| **GitHub: Trading-Strategies (Order Flow & Microstructure)** | 2026 | Sehr hoch — Multi-Plattform (MT5/MQL5 + cTrader/C#), tick-level cumulative delta, dynamic Median+MAD, VPIN/OBI/footprint imbalance, absorption, HVP | [GitHub](https://github.com/dhruuvsharma/Trading-Strategies) |

### 8. Weitere interessante Ressourcen
| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **LSTM-ARIMA Hybrid Approach in Algorithmic Investment Strategies** | 2025 | Mittel — Hybrides ML/TS-Modell (LSTM + ARIMA) auf S&P 500, FTSE 100, CAC 40 (2000–2023). Outperformt RandomForest-ARIMA und Buy&Hold | [ScienceDirect](https://www.sciencedirect.com/science/article/pii/S0950705125006094) |
| **A Profitable Algorithmic Trading Strategy: WFO Case Study (ETH)** | 2025 | Mittel — ETH WFO POC mit ML-Modell, Sharpe 5.28, Return 38.89% | [InsiderFinance](https://wire.insiderfinance.io/a-profitable-algorithmic-trading-strategy-a-walk-forward-optimization-case-study-f23ba11e0231) |
| **NextVoL White Paper: Volatility Targeting for Long-Term Compounding** | 2026 | Hoch — Systematisches Risk-Management-Overlay zur Stabilisierung des Investor-Erlebnisses | [Concretum Group](https://concretumgroup.com/nextvol-white-paper-volatility-targeting-for-long-term-compounding/) |
| **The Tranching Dilemma: Cost-Aware Approach to Mitigate Rebalance Timing Luck** | 2025 | Mittel — Beschäftigt sich mit Rebalance-Timing-Luck in Factor-Portfolios (bis 350bp Unterschied!) | [Concretum Group](https://concretumgroup.com/the-tranching-dilemma-a-cost-aware-approach-to-mitigate-rebalance-timing-luck-in-factor-portfolios/) |
| **Algorithmic Trading Strategy Development and Optimisation (S&P 500 + FinBERT)** | 2026 | Sehr hoch — Kombiniert technische Indikatoren (MA, Momentum, Volatilität) mit FinBERT-Sentimentanalyse aus Earnings Calls. Signifikante Outperformance vs. Baseline. 27 Seiten, 7 Figuren | [arXiv:2603.15848](https://arxiv.org/abs/2603.15848) |
| **Interpretable Hypothesis-Driven Trading: Walk-Forward Validation Framework** | 2025 | Sehr hoch — Stringenter WFO-Rahmen für Market-Microstructure-Signale, 34 OOS-Perioden, 100 US Equities (2015–2024). Modest aber robust: Sharpe 0.33, Max-DD -2.76%, Beta 0.058. Open-Source-Implementierung | [arXiv:2512.12924](https://arxiv.org/html/2512.12924v1) |
| **Finance-Grounded Optimization For Algorithmic Trading** | 2026 | Sehr hoch — Modifizierte Sharpe-Loss-Funktionen (ModSharpeLoss), 61 Kryptos, Daily/Hourly/15min. Adressiert Skalierungsinvarianz und Instabilität des Standard-Sharpe. Vectorized + Robustheitstests | [arXiv:2509.04541](https://arxiv.org/html/2509.04541v2) |
| **Algorithmic Trading Overfitting: Why Backtests Fail in Live Markets** | 2026 | Hoch — Praktischer Guide zu Overfitting-Erkennung (Indikatoren, Metriken, Lösungen). Betont WFO als Goldstandard | [PickMyTrade](https://blog.pickmytrade.trade/algorithmic-trading-overfitting-backtest-failure) |
| **Algorithmic Trading Metrics: Complete Guide** | 2026 | Hoch — Umfassende Erklärung von Sharpe, Sortino, Calmar, Recovery Factor, Equity Curve. Praktische Daumenregeln | [TradingWyckoff](https://tradingwyckoff.com/en/algorithmic-trading/algorithmic-trading-metrics) |
| **Walk-Forward Optimisation in Python, Step by Step** | 2025 | Hoch — HackerNoon-Tutorial: IS/OOS-Windows, Rolling Windows, Python-Code, Overfitting-Detection | [HackerNoon](https://hackernoon.com/walk-forward-optimisation-in-python-step-by-step) |
| **Walk-Forward Optimization: The Only Backtesting Method That...** | 2025 | Hoch — TradeWink Guide: WFO vs. Standard-Backtesting, IS/OOS-Setup, Ergebnis-Interpretation | [TradeWink](https://www.tradewink.com/learn/walk-forward-optimization-backtesting-guide) |
| **Walk Forward Optimization for Stronger Trading Systems** | 2025 | Hoch — MonsterTradingSystems: Schritt-für-Schritt WFO-Prozess, Robustheits-Checks | [MonsterTradingSystems](https://www.monstertradingsystems.com/walk-forward-optimization) |
| **Systematic WFO Analysis: EMA Crossover on BTCUSD** | 2025 | Hoch — 11 Rolling Windows, Regime-Erkennung, empirische Risk-Limits für Live-Deployment | [Strateda](https://www.strateda.com/articles/walk-forward-optimization-complete-guide) |
| **Walk-Forward Analysis using Ollama** | 2025 | Mittel — Nutzt lokale LLMs (Ollama) für Overfitting-Detection und Strategy-Evaluation. KI-gestützte WFO-Analyse ohne Cloud-API | [Markaicode](https://markaicode.com/walk-forward-analysis-ollama-strategy-robustness) |

---

## 📌 Für deinen Prompt an ChatGPT / Deep Research (Priorisierung)

**Top 3 für sofortige Lektüre:**
1. **WFO Novel Approach (arXiv:2602.10785)** — Dein TODO-Punkt #1. Liefert den theoretischen Rahmen + praktische Window-Optimierung.
2. **Beat the Market / SPY Intraday Momentum (SSRN 4824172)** — Sehr einfache, profitable Regel-Stratgie für SPY. Könnte direkt als Template für SP500/US100 dienen.
3. **Meta-Learning Mixture of Strategies (arXiv:2505.03659)** — Ensemble/Meta-Learning für Online Portfolio Selection. Passt zu deinem Multi-Strategy-Ansatz.

**Top 3 für Footprint/Order-Flow Integration:**
1. **GitHub Trading-Strategies (dhruuvsharma)** — Konkreter Code für cTrader + MT5, VPIN/OBI/footprint imbalance
2. **Kalena AI Order Flow Guide** — 7 Strategies, praktisch anwendbar
3. **AlgoStorm Footprint Charts** — Detaillierte Signalerklärung (Absorption, Exhaustion, Imbalance)

**Top 3 für Portfolio/Risk-Management:**
1. **Hierarchical Risk Parity (ScienceDirect 2025)** — Aktuellste HRP-Implementierung, direkt verglichen mit deinem bestehenden Code
2. **Volatility Targeting Is Trendy (SSRN 4773781)** — Erklärt den Alpha-Mechanismus von VT
| **Walk-Forward Analysis using Ollama** | 2025 | Mittel — Nutzt lokale LLMs (Ollama) für Overfitting-Detection und Strategy-Evaluation. KI-gestützte WFO-Analyse ohne Cloud-API | [Markaicode](https://markaicode.com/walk-forward-analysis-ollama-strategy-robustness) |

### 10. Machine Learning & Deep Learning für Intraday-Trading

| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **Machine Learning-based Intraday Trading Strategy (SMA + Volume + VWAP + ANN)** | 2026 | Hoch — Kombiniert SMA-Crossover, Volumen, VWAP mit Artificial Neural Network für Intraday-Signale. Getestet an aktiv gehandelten Aktien | [IJSAT](https://www.ijsat.org/research-paper.php?id=10390) |
| **Forecasting Intraday Volume in Equity Markets with Machine Learning** | 2025 | Sehr hoch — ML-Modelle (NNs, Ensemble Trees) für Intraday-Volume-Prognose. Clustering von Assets für bessere Generalisierung. VWAP-Strategie-Anwendung | [arXiv:2505.08180](https://arxiv.org/html/2505.08180v1) |
| **Multi-Horizon Echo State Network Prediction of Intraday Stock Returns** | 2025 | Hoch — Echo State Networks (ESN) für Multi-Horizon-Intraday-Return-Vorhersage. "Virtue of Complexity" Argument. Multi-Period-Portfolio-Optimierung | [PDF](https://petrosdel.github.io/publication/ballarin2025multi/ballarin2025multi.pdf) |
| **How to Build a Machine Learning Day Trading Strategy** | 2026 | Hoch — Praktischer Guide: Data Collection, Feature Engineering, ML-Modell, Backtesting, Risk Management (Stop-Loss/Take-Profit), Model Retraining | [DayTrading.com](https://www.daytrading.com/build-machine-learning-trading-strategy) |
| **Predictive Modeling of FX Trading Signals using ML & Rolling WFO** | 2025 | Hoch — ML-Modelle für Forex (daily + intraday), Vergleich mit Momentum-Strategien. Rolling Walk-Forward Optimization. Sharpe-Ratio + t-Tests | [ScienceDirect](https://www.sciencedirect.com/science/article/pii/S095741742501351X) |
| **AI in Stock Markets 2026: Smarter Algorithms, Faster Decisions** | 2026 | Mittel — Überblick: KI verarbeitet Daten, managt Risiko, führt Trades aus. Von Speed zu Intelligence. KI als Augmentation, nicht Replacement | [AnalyticsInsight](https://www.analyticsinsight.net/artificial-intelligence/ai-powered-trading-strategies-how-algorithms-are-shaping-2026-markets) |
| **Forex Algorithmic Trading Strategies in 2026** | 2026 | Mittel — 16 Forex-Algo-Strategien: Trend-Following, Mean-Reversion, Scalping, ML-Modelle, Breakout, Carry-Trade, etc. Praktische Auswahlhilfe | [GoatFundedTrader](https://www.goatfundedtrader.com/blog/forex-algorithmic-trading-strategies) |
| **Real-Time Stock Market Anomaly Detection Using ML** | 2026 | Hoch — End-to-End: Data Pipeline (Quix Streams + Kafka), Isolation Forest für Anomalie-Erkennung, Real-Time-Monitoring | [Python PlainEnglish](https://python.plainenglish.io/real-time-stock-market-anomaly-detection-using-machine-learning-an-end-to-end-data-engineering-3a7b83aeefa3) |

### 11. Sentiment Analysis & NLP für Trading

| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **Sentiment Analysis for Crypto Markets: Using NLP on News, Twitter, and On-Chain Signals** | 2026 | Hoch — Kombiniert Off-Chain (News, Social Media) mit On-Chain-Signalen. Fear & Greed Index, NLP-Pipeline für Crypto | [Blockchain Council](https://www.blockchain-council.org/cryptocurrency/sentiment-analysis-for-crypto-markets) |
| **Deep Learning and NLP in Cryptocurrency Forecasting** | 2025 | Sehr hoch — BART MNLI Zero-Shot, FinBERT, BERT für Bitcoin/Ethereum. Lokale Extrema als Target. Deep Learning schlägt Dictionary-Methoden. Code verfügbar | [ScienceDirect](https://www.sciencedirect.com/science/article/pii/S0169207025000147) |
| **Financial Market Sentiment Analysis Using LLM and RAG** | 2025 | Mittel — FinBERT + RAG-Pipeline für Sentiment-Scoring. Statistische Signifikanz getestet, aber limited predictive power (R²=0.01) | [SSRN 5145647](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5145647) |
| **Enhancing Trading Performance Through Sentiment Analysis with LLMs: S&P 500** | 2025 | Hoch — GPT-2 + FinBERT + technische Indikatoren (MACD, ARIMA, VW MACD) auf S&P 500. Beste Return 5.77% (GPT-2 + VW MACD auf Dow Jones) | [arXiv:2507.09739](https://arxiv.org/abs/2507.09739) |
| **NLP-Powered Sentiment Analysis in Trading Strategy (QuantConnect)** | — | Mittel — Regelbasierter Dictionary-Ansatz, Insight-Weight-Portfolio-Konstruktion, Live-Trading-fähig auf QuantConnect | [Atlantis Press PDF](https://www.atlantis-press.com/article/125989724.pdf) |
| **Sentiment Trading with Large Language Models** | 2024 | Hoch — 965k Nachrichten (2010–2023), BERT, OPT, FinBERT, Loughran-McDonald. OPT (GPT-3) Accuracy 74.4%, BERT 72.5%, FinBERT 72.2% | [arXiv:2412.19245](https://arxiv.org/abs/2412.19245) |

### 12. Risk Management & Portfolio Protection

| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **AI Risk Management: How Algorithms Protect Your Portfolio** | 2026 | Sehr hoch — Dynamic Position Sizing (bis 45% Drawdown-Reduktion), ATR-basierte Stop-Losses, Drawdown Circuit Breakers, Hidden Markov Models für Regime-Erkennung | [TradeAlgo](https://www.tradealgo.com/trading-guides/ai-trading/ai-risk-management-guide) |
| **Risk Management in Algorithmic Trading: Position Sizing, Drawdown Control, Portfolio Protection** | 2026 | Sehr hoch — Institutioneller Guide: Pre-Trade, Intraday, System-Level-Risikocontroller. Korrelationrisiko, Convexity, Operational Risk | [Braxton Tulin](https://braxtontulin.com/risk-management-algorithmic-trading-position-sizing-drawdown-portfolio-protection) |
| **Algorithmic Trading Risk Management: Position Sizing** | 2025 | Hoch — TradeWink Guide: Pre-Trade Checks, Position Sizing (Fixed Fractional, ATR, Kelly), Correlation Limits, Kill Switches | [TradeWink](https://www.tradewink.com/learn/algorithmic-trading-risk-management) |
| **Machine Learning for Finance: Portfolio Optimization, Risk Management, and Algorithmic Trading** | 2024 | Mittel — Ganzheitlicher Ansatz: Markowitz, Supervised Learning für Risk, RL für Algo-Trading. Python/Jupyter/Colab | [IJSDR PDF](https://www.ijsdr.org/papers/IJSDR2408077.pdf) |
| **Risk-aware Trading Portfolio Optimization (RATPO)** | 2025 | Hoch — Particle Swarm Optimization für Portfolio-Optimierung mit VaR + P&L-Objective. Echt-Trading-Portfolio mit hunderten Instrumenten | [arXiv:2503.04662](https://arxiv.org/abs/2503.04662) |
| **The Optimal Investment Portfolio (Sharpe vs. Risk Parity vs. 60/40)** | — | Hoch — Vergleich: 100% Stocks (Sharpe 0.41), 60/40 (Sharpe 0.53), Risk Parity (Sharpe 0.65). Risk Parity niedrigster Max-DD (15%) | [Kardinal Financial](https://kardinalfinancial.com/blog/the-optimal-investment-portfolio) |
| **Machine Learning Optimization Algorithms & Portfolio Allocation** | — | Hoch — Amundi Research: Coordinate Descent, ADMM, Proximal Gradient, Dykstra's Algorithm für large-scale Portfolio-Optimierung | [Amundi](https://research-center.amundi.com/article/machine-learning-optimization-algorithms-portfolio-allocation) |

### 13. Quantitative Finance Blogs & Lern-Ressourcen

| Ressource | Fokus | Relevanz | Link |
|---|---|---|---|
| **QuantInsti Blog** | Algorithmic Trading, WFO, Python, Praxistutorials | Sehr hoch — Große Sammlung an Guides, Schritt-für-Schritt-Implementierungen | [quantinsti.com/blog](https://blog.quantinsti.com/) |
| **Concretum Group Research** | Momentum, Volatility Targeting, Rebalance Timing, Intraday-Alphas | Sehr hoch — Praktische Papers mit konkreten Zahlen (Sharpe, DD, Returns) | [concretumgroup.com](https://concretumgroup.com/) |
| **Meyers Analytics Working Papers** | WFO für Futures/FX/Stocks, Repeated Median Velocity, Fixed Memory Polynomial, EPFFT | Sehr hoch — Sehr detaillierte, praxisnahe WFO-Papers | [meyersanalytics.com/papers.html](https://meyersanalytics.com/papers.html) |
| **HackerNoon Algo Trading** | WFO Python, Overfitting, Strategy Validation | Hoch — Technische Tutorials mit Code | [hackernoon.com](https://hackernoon.com/tag/algorithmic-trading) |
| **TradeWink** | Risk Management, WFO, Position Sizing | Hoch — Praktische Guides für Retail und Institutionell | [tradewink.com](https://www.tradewink.com/) |
| **InsiderFinance Wire** | WFO Case Studies, ML Trading, Krypto/Stocks | Hoch — Konkrete Implementationen mit GitHub-Code | [wire.insiderfinance.io](https://wire.insiderfinance.io/) |
| **Wayland Zhang Quant Book** | Framework-Vergleich (Backtrader, VectorBT, Zipline, FinRL), AI Quant Trading | Hoch — Sehr gut strukturiert, Open-Source Book | [waylandz.com/quant-book-en](https://waylandz.com/quant-book-en/) |

### 14. Reinforcement Learning (RL) & DRL Trading Frameworks & Papers

| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **FinRL (AI4Finance Foundation)** | laufend | Sehr hoch — Erstes Open-Source-Framework für Financial RL. PPO, SAC, DDPG, A2C, TD3. Stocks, Crypto, Forex. Live-Trading, Portfolio-Optimierung | [GitHub](https://github.com/AI4Finance-Foundation/FinRL) |
| **FinRL-X / FinRL-Trading** | laufend | Sehr hoch — Next-Gen: AI-native, modular, produktionsorientiert. Für Live-Deployment | [GitHub](https://github.com/AI4Finance-Foundation/FinRL-Trading) |
| **FinGPT** | laufend | Sehr hoch — Open-Source Financial LLM für Sentiment, Forecasting, Fine-Tuning | [GitHub](https://github.com/AI4Finance-Foundation/FinGPT) |
| **FinRobot** | laufend | Sehr hoch — AI Agent Platform für Financial Analysis. Multi-Agent-Workflows | [GitHub](https://github.com/AI4Finance-Foundation/FinRobot) |
| **Gym Trading Environment** | laufend | Hoch — Einfache, schnelle Trading-Gym für RL-Agenten. Short/Margin, Multi-Exchange, High-Performance-Rendering | [GitHub](https://github.com/ClementPerroud/Gym-Trading-Env) |
| **JaxMARL-HFT** | 2025 | Sehr hoch — GPU-accelerated Multi-Agent RL für HFT. JAX-basiert, LOBSTER-Daten, Market Making, Execution, Directional Trading | [GitHub](https://github.com/vmohl/JaxMARL-HFT) |
| **TradeMaster** | 2025 | Hoch — RL-empowered Plattform für Quantitative Trading. Strategy Research & Simulation | (kein direkter Repo-Link verifiziert, siehe Grokipedia) |
| **Deep RL for Automated Stock Trading: Ensemble Strategy** | 2025 | Sehr hoch — Ensemble PPO, A2C, DDPG. 30 Dow Jones-Aktien. Sharpe verbessert. Open-Source FinRL | [arXiv:2511.12120](https://arxiv.org/abs/2511.12120) |
| **Deep RL for Trading** (Oxford) | 2019 | Sehr hoch — DQN/DRL auf 50 Futures (2011–2019). Übertrifft Time-Series-Momentum. Volatility Scaling integriert | [arXiv:1911.10107](https://arxiv.org/abs/1911.10107) |

### 15. Limit Order Book (LOB) & Market Microstructure

| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **Market Microstructure: Order Books & Execution Mechanics** | 2026 | Sehr hoch — Interaktiver Guide: Order Book Architektur, Matching-Algorithmen, Order-Typen, Liquiditätsanalyse, Execution-Logik | [Michael Brenndoerfer](https://mbrenndoerfer.com/writing/market-microstructure-order-book-mechanics) |
| **Limit Order Book Dynamics in Matching Markets** | 2025 | Hoch — Mikrostruktur-Framework mit rigidem Bid-Ask-Spread, latenter Präferenzmatrix, Threshold Impossibility Theorem, dynamisches Discrete-Choice-Execution-Modell | [arXiv:2511.20606](https://arxiv.org/abs/2511.20606) |
| **Deep Limit Order Book Forecasting: LOBFrame** | 2025 | Sehr hoch — Open-Source-Codebase für LOB-Verarbeitung. Deep Learning Modelle (DeepLOB, TransLOB, LiT). Operativer Framework für Trading-Signale | [Frontiers / arXiv](https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2025.1616485/full) |
| **LiT: Limit Order Book Transformer** | 2025 | Sehr hoch — Transformer für LOB ohne Convolution. Räumliche + temporale Abhängigkeiten. Schnelles Fine-Tuning. Getestet auf Binance | [Frontiers](https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2025.1616485/full) |
| **High-Frequency Trading in a Limit Order Book** | 2006 | Klassiker — Avellaneda & Stoikov. Inventory-basiertes Market-Making. Indifferenz-Bewertung, Bid/Ask-Quote-Optimierung | [PDF](https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf) |
| **Limit Order Book Simulations: A Review** | 2024 | Hoch — Umfassender Review: LOB-Modelle, Simulationen, KI-Techniken, Calibration für Automated Trading | [arXiv:2402.17359](https://arxiv.org/abs/2402.17359) |
| **Dynamic Modeling of LOB and Market Maker** | 2025 | Hoch — Markov-Queue-Theorie für LOB, optimale Market-Maker-Strategie, optimal quote deviation | [MDPI Mathematics](https://www.mdpi.com/2227-7390/13/5/778) |
| **Order Book Imbalances: Practical Guide for Day Traders** | 2026 | Hoch — OBI-Berechnung (Top 5-10 Levels), actionable short-term price pressure, Liquiditätsanalyse | [QuantStrategy.io](https://quantstrategy.io/blog/order-book-imbalances-a-practical-guide-for-day-traders) |
| **Market Microstructure Foundations** (Trading Strategy docs) | — | Hoch — Order-driven vs. quote-driven, Market Maker, Price Discovery, Information Asymmetry, Bid-Ask Spread | [Trading Strategy](https://tradingstrategy.ai/docs/learn/market-microstructure.html) |

### 16. Alternative Data & Alpha-Generierung

| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **Satellite Imagery for Trading: Complete Guide** | 2026 | Hoch — Parkplatz-Zählungen, Öl-Lager, Crop Health, Shipping Traffic. Computer Vision, Python-Pipeline | [PapersWithBacktest](https://paperswithbacktest.com/datasets/satellite-imagery-trading) |
| **Alternative Data in Quantitative Trading** | 2026 | Sehr hoch — Satelliten, Credit Card, Web Scraping, Social Sentiment. 65% der Hedge Funds nutzen es. Bis 3% höhere Returns | [Braxton Tulin](https://braxtontulin.com/alternative-data-quantitative-trading-satellite-sentiment-social-data-sources) |
| **Alternative Data Practical Integration** | 2026 | Hoch — Satellite + Credit Card + Social Sentiment Case Study. Vector Databases für Multi-Modal-Suche (50 TB/Woche) | [Algo Lab](https://algolabhk.com/en/blogs/alternative-data-quantitative-trading-satellite-credit-card-2025-en) |
| **Alternative Data for Algorithmic Trading: What Works?** | 2025 | Hoch — Social Media 87% Forecast Accuracy, Transaction Data 10% improvement, Satellite 18% better earnings estimates | [LuxAlgo](https://www.luxalgo.com/blog/alternative-data-for-algorithmic-trading-what-works) |
| **Alternative Data Integration: From Satellite to Social** | 2025 | Hoch — Vector DB Architektur für 23 alternative Datenquellen. Semantic Search, Multi-Modal | [Orchard Labs](https://www.orchardlabs.ai/articles/alternative-data-integration/) |
| **7 Alternative Data Sources Reshaping Investment Decisions** | 2025 | Mittel — ESG, Social Sentiment, Satellite, Credit Card, Web Traffic, Patent Data, Weather/Climate | [Tickertrends](https://blog.tickertrends.io/p/7-alternative-data-sources-reshaping-investment-decisions) |

### 17. Weitere nützliche Ressourcen

| Ressource | Fokus | Relevanz | Link |
|---|---|---|---|
| **AI4Finance Foundation** | FinRL, FinGPT, FinRobot, Open-Source Financial AI | Sehr hoch — 120+ Contributors, 50+ Länder, 30+ Papers | [ai4finance.org](https://ai4finance.org/) |
| **QuantBook (Wayland Zhang)** | Backtrader, VectorBT, Zipline, FinRL Vergleich, AI Quant Trading | Hoch — Strukturiertes Open-Source Book | [waylandz.com/quant-book-en](https://waylandz.com/quant-book-en/) |
| **awesome-systematic-trading** | Curated List: Crypto, Stock, Futures, Options, CFDs, FX | Sehr hoch — 3k Stars, umfassend | [GitHub](https://github.com/wangzhe3224/awesome-systematic-trading) |
| **Grokipedia: Popular open-source quant projects 2025-2026** | Qlib (39k), Qbot (16.7k), Abu (16.6k), VeighNa, TradeMaster | Sehr hoch — Marktübersicht Open-Source-Quant | [Grokipedia](https://grokipedia.com/page/Popular_open-source_quantitative_trading_projects_20252026) |
| **arXiv q-fin.TR (Trading and Market Microstructure)** | Neueste Papers zu Trading, Market Microstructure | Sehr hoch — Tägliche Updates | [arxiv.org/q-fin.TR](https://arxiv.org/list/q-fin.TR/recent) |
| **PapersWithBacktest** | Alternative Daten, Strategien, Frameworks | Hoch — Praktische Guides mit Code | [paperswithbacktest.com](https://paperswithbacktest.com/) |
| **Digital AI in Finance 2026 (Workshop)** | Alternative Data, LLMs, Explainable AI, Blockchain, Risk Management | Hoch — Konferenz-Track | [digital-ai-finance.github.io](https://digital-ai-finance.github.io/digital-ai-in-finance/topic_altdata.html) |

### 18. Python Libraries & Tools für Algorithmic Trading

| Library / Tool | Sprache | Fokus | Relevanz | Link |
|---|---|---|---|---|
| **pandas-ta** | Python | 150+ TA-Indikatoren, 60 Candlestick-Patterns (mit TA-Lib), Numba-optimiert | Sehr hoch — Aktive Entwicklung, Pandas DataFrame Extension | [GitHub](https://github.com/twopirllc/pandas-ta) |
| **pandas-ta-classic** | Python | 192 Indikatoren, 62 native Candlestick-Patterns, Community-maintained Fork | Sehr hoch — Stabil, kein TA-Lib benötigt | [GitHub](https://github.com/xgboosted/pandas-ta-classic) |
| **TA-Lib** | C / Python Wrapper | 200+ Indikatoren (ADX, MACD, RSI, Stochastic, BB), Candlestick Patterns | Sehr hoch — Industriestandard, BSD-Lizenz | [ta-lib.org](https://ta-lib.org/) |
| **VectorBT** | Python | Vectorisiertes Backtesting, 100-1000x schneller, Parameter-Sweeps, Visualisierung | Sehr hoch — Ideal für Research | [GitHub](https://github.com/polakowo/vectorbt) |
| **Backtrader** | Python | Event-driven Backtesting, Live-Trading, große Community | Sehr hoch — Flexibel, aber Entwicklung verlangsamt | [backtrader.com](https://www.backtrader.com/) |
| **QuantConnect LEAN** | C# / Python | Institutionell, Multi-Asset, Cloud + Local, 40+ Broker | Sehr hoch — Full-Stack | [GitHub](https://github.com/QuantConnect/Lean) |
| **FinRL** | Python | Financial Reinforcement Learning, PPO/SAC/DDPG/A2C, Live-Trading | Sehr hoch — Open-Source, AI4Finance | [GitHub](https://github.com/AI4Finance-Foundation/FinRL) |
| **Freqtrade** | Python | Crypto Trading Bot, Backtesting + Live, Docker, Telegram/WebUI | Hoch — Crypto-Fokus | [GitHub](https://github.com/freqtrade/freqtrade) |
| **NautilusTrader** | Rust + Python | Hochperformant, Event-driven, Multi-Asset, Produktionssysteme | Sehr hoch — Rust-Core | [nautilustrader.io](https://nautilustrader.io/) |
| **QLib (Microsoft)** | Python | 39k Stars, AI-native Quant Plattform, Modelle für Aktien/Crypto | Sehr hoch — Microsoft Research | [GitHub](https://github.com/microsoft/qlib) |
| **Qbot** | Python | 16.7k Stars, KI-gestützter Quant-Bot, A-Share Fokus | Sehr hoch — Chinesischer Markt | [GitHub](https://github.com/)(siehe Grokipedia) |
| **Abu (阿布量化)** | Python | 16.6k Stars, Chinesisches Quant-Framework, A-Share | Sehr hoch — Chinesischer Markt | [GitHub](https://github.com/)(siehe Grokipedia) |
| **VeighNa (vn.py)** | Python | Event-driven, Multi-Market, besonders chinesische Community | Hoch — Flexible Architektur | [GitHub](https://github.com/vnpy/vnpy) |
| **PyAlgoTrade** | Python | Einfaches Event-driven Backtesting | Mittel — Weniger aktiv | [GitHub](https://github.com/gbeced/pyalgotrade) |
| **Zipline** | Python | Quantopian-Erbe, Factor-Research, Alphalens | Mittel — In Maintenance | [GitHub](https://github.com/quantopian/zipline) |
| **Empyrial** | Python | AI/Data-Driven Portfolio-Management, Risk/Performance | Mittel — Gut für Research | [GitHub](https://github.com/santoshlite/EigenLedger) |
| **LumiBot** | Python | Backtesting + Trading Bots, Multi-Asset | Hoch — Einfache Bedienung | [GitHub](https://github.com/Lumiwealth/lumibot) |
| **QuantLib** | C++ / Python | Pricing, Yield-Kurven, Volatility-Modelle | Sehr hoch — Standard für Derivatives | [quantlib.org](https://www.quantlib.org/) |
| **Optopsy** | Python | Options-Research und Backtesting | Mittel — Spezialisiert | [GitHub](https://github.com/goldspanlabs/optopsy) |
| **hftbacktest** | Rust | High-Frequency, Market-Making, Level-2/3 Orderbook, Tick-Daten | Sehr hoch — Für echte HFT | [GitHub](https://github.com/nkaz001/hftbacktest) |
| **barter-rs** | Rust | Event-driven Live-Trading & Backtesting | Hoch — Rust-Performance | [GitHub](https://github.com/barter-rs/barter-rs) |
| **JaxMARL-HFT** | Python/JAX | GPU-accelerated Multi-Agent RL für HFT, LOBSTER-Daten | Sehr hoch — Cutting-Edge | [GitHub](https://github.com/vmohl/JaxMARL-HFT) |
| **Gym Trading Env** | Python | Trading-Gym für RL-Agenten, Short/Margin, Multi-Exchange | Hoch — Einfache Integration | [GitHub](https://github.com/ClementPerroud/Gym-Trading-Env) |
| **QuestDB** | Java/C++ | Time-Series DB, optimiert für Marktdaten, TWAP/VWAP | Hoch — High-Throughput | [questdb.io](https://questdb.io/) |
| **Quix Streams** | Python | Real-Time Data Pipeline, Kafka-Integration, Streaming | Hoch — Für Live-Trading | [GitHub](https://github.com/quixio/quix-streams) |
| **yfinance** | Python | Yahoo Finance API, kostenlose historische Daten | Sehr hoch — Einfach, kostenlos | [PyPI](https://pypi.org/project/yfinance/) |
| **ccxt** | Python/JS | Crypto Exchange API, 200+ Exchanges | Sehr hoch — Standard für Crypto | [GitHub](https://github.com/ccxt/ccxt) |
| **alpaca-py** | Python | Alpaca API (Stocks, Crypto), Paper/Live Trading | Sehr hoch — Einfache Broker-Integration | [GitHub](https://github.com/alpacahq/alpaca-py) |
| **ib_insync** | Python | Interactive Brokers API, asynchron, umfassend | Sehr hoch — IB Standard | [GitHub](https://github.com/erdewit/ib_insync) |

### 19. Execution Algorithms & Order Management

| Ressource | Fokus | Relevanz | Link |
|---|---|---|---|
| **Algorithmic Trading With TWAP and VWAP Using Alpaca** | Praktische Implementierung: VWAP/TWAP-Berechnung, Alpaca API, Live-Paper-Trading | Sehr hoch — Schritt-für-Schritt Code | [Alpaca Blog](https://alpaca.markets/learn/algorithmic-trading-with-twap-and-vwap-using-alpaca) |
| **Algorithmic VWAP Trading Implementation** | VWAP-Strategie mit Options (Long/Short), Interactive Brokers REST API, SPY Backtest | Hoch — Konkrete Implementation | [FIT Solutions](https://www.fitsolutionsusa.com/blog/algorithmic-vwap-trading-implementation) |
| **Execution Algorithms for Futures Trading** | TWAP, VWAP, Iceberg, Sniper, Implementation Shortfall, POV, Market Impact | Sehr hoch — Institutioneller Guide für Futures | [NexusFi](https://nexusfi.com/a/automation/execution-algorithms) |
| **TWAP vs VWAP Price Algorithms** | Theorie: TWAP (zeitbasiert) vs VWAP (volumengewichtet), DeFi-Anwendung, Vor-/Nachteile | Hoch — Klare Erklärung | [Chainlink](https://chain.link/education-hub/twap-vs-vwap) |
| **TWAP (QuestDB Glossary)** | Definition, Formel, TWAP vs VWAP, Market Impact, Performance-Metriken | Hoch — Präzise Definition | [QuestDB](https://questdb.com/glossary/time-weighted-average-price-twap) |
| **Order Execution Algorithms: TWAP, VWAP, IS** | NordVarg Blog: Theorie + praktische Überlegungen | Mittel — Kompakt | [NordVarg](https://nordvarg.com/blog/order-execution-algorithms) |
| **Wyden Execution Algorithms** | TWAP, VWAP, POV, Sniper, Iceberg, Trailing Limit, Market Sweep, Target Position | Hoch — Professionelle Suite | [Wyden](https://www.wyden.io/enterprise/crypto-trading-oems/execution-algorithms) |
| **VWAP Backtrader** | VWAP-Indikator für Backtrader | Mittel — Nützlich für Backtrader-Nutzer | [GitHub](https://github.com/eslazarev/vwap-backtrader) |
| **GitHub vwap topic** | Sammlung von VWAP/TWAP/LOB-Projekten (C++, Python, Go) | Hoch — Code-Beispiele | [GitHub Topics](https://github.com/topics/vwap) |

### 20. Wichtige Blogs & Lern-Ressourcen (Fortsetzung)

| Ressource | Fokus | Relevanz | Link |
|---|---|---|---|
| **QuantInsti Blog** | Algo Trading, WFO, Python, ML Trading, Risk Parity, Donchian Channels | Sehr hoch — Praxistutorials, Webinar-Archiv | [blog.quantinsti.com](https://blog.quantinsti.com/) |
| **The Robust Trader** | 64 Quant Blogs, Strategien, Risk Management, Overfitting | Sehr hoch — Kuratierte Liste | [therobusttrader.com](https://therobusttrader.com/algorithmic-trading-blog-algo-quantitative-strategies/) |
| **PredictNow.ai Blog** | Financial ML, Transformers, RL, Feature Engineering, LLMs | Sehr hoch — Dr. Ernest Chan, Cutting-Edge | [predictnow.ai/blog](https://predictnow.ai/blog) |
| **Concretum Group Research** | Momentum, Volatility Targeting, Rebalance Timing, Intraday-Alphas | Sehr hoch — Konkrete Zahlen | [concretumgroup.com](https://concretumgroup.com/) |
| **Meyers Analytics Working Papers** | WFO für Futures/FX/Stocks, Repeated Median Velocity, Fixed Memory Polynomial | Sehr hoch — Sehr detailliert | [meyersanalytics.com/papers.html](https://meyersanalytics.com/papers.html) |
| **InsiderFinance Wire** | WFO Case Studies, ML Trading, Krypto/Stocks | Hoch — Mit Code | [wire.insiderfinance.io](https://wire.insiderfinance.io/) |
| **TradeWink** | Risk Management, WFO, Position Sizing | Hoch — Praktisch | [tradewink.com](https://www.tradewink.com/) |
| **HackerNoon Algo Trading** | WFO Python, Overfitting, Strategy Validation | Hoch — Mit Code | [hackernoon.com/tag/algorithmic-trading](https://hackernoon.com/tag/algorithmic-trading) |
| **QuantStrategy.io** | Order Book Imbalances, Market Microstructure, Day Trading | Hoch — Praktische Guides | [quantstrategy.io](https://quantstrategy.io/) |
| **PapersWithBacktest** | Alternative Daten, Strategien, Frameworks, Code | Hoch — Praktisch | [paperswithbacktest.com](https://paperswithbacktest.com/) |
| **best-of-algorithmic-trading** | 110 Open-Source-Projekte, 320k Stars, 7 Kategorien, wöchentlich aktualisiert | Sehr hoch — Best-of List | [GitHub](https://github.com/merovinh/best-of-algorithmic-trading) |
| **awesome-systematic-trading** | 3k Stars, umfassende Quant-Ressourcen | Sehr hoch — Kuratiert | [GitHub](https://github.com/wangzhe3224/awesome-systematic-trading) |

### 21. Spezifische Strategien: Momentum, Mean Reversion, Pairs Trading, Stat Arb

| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **Momentum Trading Strategy: How to Build and Test in Python** | 2025 | Sehr hoch — Cross-Sectional Momentum, Jegadeesh & Titman, S&P 500 Sector ETFs, Formation Period (3-12M), Holding Period (1-3M), Risikomanagement | [QuantVero](https://www.quantvero.com/algo-trading/momentum-trading-strategy/) |
| **Simple Momentum Strategy (GitHub)** | — | Hoch — Einfache Python-Implementierung: Rolling Average Returns, yfinance, Buy-and-Hold-Vergleich | [GitHub](https://github.com/Minty-2004/Simple-Momentum-Strategy) |
| **Code a Momentum Trading Strategy in Python (2025 Guide)** | 2025 | Hoch — Schritt-für-Schritt: S&P 500, 12-Monats-Returns (letzten Monat ausgeschlossen), Survivorship-Bias-Handling, Monthly Rebalancing | [LilysAI](https://lilys.ai/en/notes/quant-investment-strategy-20251214/momentum-trading-python-code) |
| **Momentum Trading Strategies — Complete Backtesting Guide** | — | Hoch — 10/50 EMA Crossover (52% Win-Rate, 1.7:1 R/R, 11% annual), RSI Momentum, MACD, Risk Management | [BacktestMe](https://backtestme.com/guides/momentum-trading-strategies) |
| **Momentum vs Mean Reversion in Crypto** | — | Mittel — Testet Lookback/Holding-Periods, Korrelationskoeffizient, 25 Coins (30min, 2021), E.P. Chan Methodologie | [Financial Noob](https://financialnoob.substack.com/p/momentum-vs-mean-reversion-in-crypto-f62) |
| **Systematic Crypto Trading: Momentum, Mean Reversion & Volatility** | 2025 | Hoch — Z-Score Momentum, Volatility-Filtered Momentum, BTC-Neutral Residual Mean Reversion. Bayesian Optimization | [Medium](https://medium.com/@briplotnik/systematic-crypto-trading-strategies-momentum-mean-reversion-volatility-filtering-8d7da06d60ed) |
| **Momentum-Quality Small-Cap Strategy — June 2026 Update** | 2026 | Hoch — Momentum + Quality Faktoren, Small-Cap, Backtest-to-Live, Execution, Kosten | [Stock Market Pulse](https://stock-market-pulse.contentwave.net/article/momentumquality-smallcap-strategy-practical-update-june-2026) |
| **Mean Reversion Trading Strategy: How to Build and Test** | 2026 | Sehr hoch — Bollinger Bands, RSI, Pairs Trading, Statistische Tests (ADF, Ornstein-Uhlenbeck), Python-Code | [QuantVero](https://www.quantvero.com/algo-trading/mean-reversion-trading-strategy) |
| **Backtest a Mean Reversion Strategy (Quick and Easy)** | 2025 | Hoch — Monatliche Mean Reversion, Python-Pipeline, Custom Factor, Volatility Weighting | [PyQuant News](https://www.pyquantnews.com/the-pyquant-newsletter/backtest-a-mean-reversion-strategy-quick-and-easy) |
| **Mean Reversion Magic: Unleash Statistical Arbitrage with Python** | — | Hoch — Stationarity, Cointegration, Z-Score, Market Neutrality, Python-Implementierung | [Python PlainEnglish](https://python.plainenglish.io/mean-reversion-magic-unleashing-statistical-arbitrage-in-python-c9e8426ce219) |
| **Statistical Arbitrage From Scratch** | — | Hoch — Z-Score-Normalisierung, Rolling Window, Market Neutralität, FAANG-Pairs, Backtest | [Python PlainEnglish](https://python.plainenglish.io/statistical-arbitrage-from-scratch-a5aaf6e0711d) |
| **Statistical Arbitrage / Pairs Trading (GitHub)** | — | Hoch — Engle-Granger Cointegration, ADF-Test, Spread-Backtest, VectorBT, S&P 500 | [GitHub](https://github.com/ReiannaLiu/Statistical_Arbitrage_Mean_Reversion_Strategy) |
| **Pairs Trading Mean Reversion Strategy — Forex (GitHub)** | — | Hoch — Ornstein-Uhlenbeck-Prozess, Cointegration, IB API, Jupyter Notebook | [GitHub](https://github.com/LucaCereghetti/Pairs-Trading-Mean-Reversion-Strategy) |
| **Build a Pairs Trading Strategy in Python: Step-by-Step** | — | Hoch — CME WTI vs ICE Brent, Engle-Granger, sklearn Linear Regression, databento Daten | [Databento Medium](https://medium.databento.com/build-a-pairs-trading-strategy-in-python-a-step-by-step-guide-dcee006e1a50) |
| **Arbitrage Stock Trading with Python** | — | Hoch — Triangular Arbitrage, Statistical Arbitrage, Pairs Trading, Implementation mit Python | [Trading Strategies Academy](https://trading-strategies.academy/archives/1324) |
| **Mean Reversion Strategy Pack (12 Strategien)** | — | Mittel — 12 einzigartige Mean-Reversion-Strategien, Python-Code, Sample-Backtests, PDF-Manual | [PyQuantLab](https://www.pyquantlab.com/downloads/Mean-Reversion%20Strategies.html) |
| **Using Python for Statistical Arbitrage** | — | Hoch — Cointegration, ADF-Test, Pair-Selektion, yfinance, statsmodels, scikit-learn | [PyQuant News](https://www.pyquantnews.com/free-python-resources/using-python-for-statistical-arbitrage) |

### 22. Crypto Trading Bots & Frameworks (Open-Source)

| Bot / Framework | Sprache | Stars | Relevanz | Link |
|---|---|---|---|---|
| **Freqtrade** | Python | 51.7k | Sehr hoch — Crypto Trading Bot, Backtesting, Hyperopt (ML-Optimierung), Telegram/WebUI, 200+ Exchanges, FreqAI | [GitHub](https://github.com/freqtrade/freqtrade) |
| **Hummingbot** | Python | 4.3k | Sehr hoch — Market Making, HFT, CEX/DEX, $36B+ Volume, 300+ Connectors, Condor (AI), Apache 2.0 | [GitHub](https://github.com/hummingbot/hummingbot) |
| **Jesse** | Python | — | Hoch — Einfache Installation, unified Codebase (Research → Backtest → Paper → Live), skalierbar (1000+ Routes) | [jesse.trade](https://jesse.trade/) |
| **Gainium** | Python | — | Hoch — No-Code, Grid + DCA Hybrid, Backtester, Trading Journal, Open-Source CE + Paid Cloud | [gainium.io](https://gainium.io) |
| **OctoBot** | Python | 4.3k | Hoch — Automated Trading, ML/AI, Portfolio Management, Real-Time Data | [GitHub](https://github.com/Drakkar-Software/OctoBot) |
| **ZenBot** | Node.js | — | Mittel — Command-Line Crypto Trading Bot, Crossover, MACD, RSI, Bollinger | [GitHub](https://github.com/DeviaVir/zenbot) |
| **Stoic AI** | Python | — | Mittel — Automated Crypto Portfolio, KI-gesteuert, CEX/DEX | [stoic.ai](https://stoic.ai/) |

### 23. Spezifische Strategien: Trend-Following, Turtle Trading, KAMA, Breakout

| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **PyTrendFollow — Systematic Futures Trading** | laufend | Sehr hoch — ~20% p.a. bei 25% Volatilität, Trend-Following für Futures, IB-Integration, automatisches Contract-Rolling, Jupyter R&D | [GitHub](https://github.com/chrism2671/PyTrendFollow) |
| **Trend-Following Strategy Pack (21 Strategien)** | — | Sehr hoch — ATR-Scaled Trailing-Stop, Composite Price-Volume Momentum, Dual Momentum, Kalman-Filter, HMM Regime-Adaptive, Renko Breakout, EVT-Adjusted, PCA Momentum | [PyQuantLab](https://www.pyquantlab.com/downloads/Trend-Following%20Strategies.html) |
| **QuantConnect: Commodities Futures Trend Following** | 2014 (Paper) | Hoch — Implementiert "Two Centuries Of Trend Following" (Lempérière et al.) auf Commodity Futures | [GitHub](https://github.com/QuantConnect/Tutorials/blob/master/04%20Strategy%20Library/357%20Commodities%20Futures%20Trend%20Following/01%20Abstract.html) |
| **Commodity Trend Following: Complete Guide** | 2025 | Hoch — CTA-Logik, CFDs, Entry/Exit, Position Sizing, Drawdown-Management | [VantoTrade](https://vantotrade.com/commodities/trend-following) |
| **Implementing Commodity Trend Following in Python** | 2024 | Hoch — MACD-basiert, Python-Code, Rohstoffe (Gold, Öl, Kupfer) | [PlainEnglish](https://plainenglish.io/python/implementing-commodity-trend-following-strategies-in-code) |
| **Turtle Trading Strategy in Python** | 2026 | Hoch — Donchian Breakout, ATR-Position-Sizing, Trailing-Stop, Backtesting, IB-Integration | [Trading Strategies Academy](https://trading-strategies.academy/archives/3978) |
| **KAMA Strategy: Kaufman’s Adaptive Moving Average** | 2025 | Hoch — Trend-Following, Efficiency Ratio (ER), adaptive Sensitivität, weniger Whipsaws als SMA, SPY Test | [Pineify](https://pineify.app/resources/blog/master-the-kama-strategy-kaufmans-adaptive-moving-average-for-enhanced-trading-performance) |
| **Kaufman’s Adaptive Moving Average (KAMA) In Python** | 2023 | Hoch — Python-Implementierung, Pine Script Vergleich, Filter (Threshold, Zeit) | [Medium](https://medium.com/@basics.machinelearning/kaufmans-adaptive-moving-average-kama-in-python-d28328c9ee8) |
| **Adaptive Moving Average: AMA/KAMA Trading Guide & Python** | 2023 | Hoch — Formel, SC-Berechnung, Python-Code, yfinance + mplfinance, SPY Backtest | [AlphaSquawk](https://alphasquawk.com/adaptive-moving-average-ama-origins-uses-and-coding-examples) |
| **Simulated Annealing KAMA Trading Strategy (GitHub)** | — | Mittel — Optimiert KAMA-Parameter mit Simulated Annealing | [GitHub](https://github.com/Brianhulela/simulated_annealing_kaufman_adaptive_moving_average) |
| **Kaufman Efficiency Ratio (TrendSpider)** | — | Hoch — ER-Berechnung, Interpretation (0-1), Trend-Stärke, Noise-Filter | [TrendSpider](https://trendspider.com/learning-center/kaufman-efficiency-ratio) |

### 24. Options, Futures & Derivatives Trading

| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **Option Greeks Strategies & Backtesting in Python (GitHub)** | — | Hoch — Delta, Gamma, Theta, Vega, Delta-Hedging, Gamma-Scalping, Butterfly, Ratio, Nifty/USDINR/Stocks | [GitHub](https://github.com/OptionsnPython/Option-strategies-backtesting-in-Python) |
| **Options Backtesting in Python: Introductory Walkthrough** | 2023 | Hoch — Grundlagen Options, Backtesting-Umgebung, Bibliotheken | [Pipekit](https://pipekit.io/blog/options-backtesting-in-python-an-introductory-walkthrough) |
| **QuantConnect Tutorials — Options, Futures, Equities** | laufend | Sehr hoch — Umfassende Tutorials für alle Asset-Klassen, C#/Python | [GitHub](https://github.com/QuantConnect/Tutorials) |
| **FuturesBacktest** | — | Hoch — Web-Plattform für Futures-Portfolio-Backtesting ohne Code | [futuresbacktest.com](https://www.futuresbacktest.com/) |
| **IASG Trend Following Strategy Index** | — | Hoch — CTA-Performance, Managed Futures, YTD, Inception-Daten | [iasg.com](https://www.iasg.com/indexes/trend-following-index) |

### 25. Weitere Python-Bibliotheken & Tools

| Library / Tool | Sprache | Fokus | Relevanz | Link |
|---|---|---|---|---|
| **Backtrader** | Python | Event-driven Backtesting, Live-Trading, 120+ Indikatoren | Sehr hoch — Stabile Basis für Prototyping | [backtrader.com](https://www.backtrader.com/) |
| **Backtesting.py** | Python | Einfaches vectorisiertes Backtesting, schöne Charts | Hoch — Gut für Einsteiger | [GitHub](https://github.com/kernc/backtesting.py) |
| **VectorBT** | Python | 100-1000x schneller als event-driven, Numba/Rust, ML-ready | Sehr hoch — Für Parameter-Sweeps | [vectorbt.dev](https://vectorbt.dev/) |
| **QuantConnect LEAN** | C#/Python | Institutionell, Multi-Asset, Cloud + Local, 40+ Broker | Sehr hoch — Full-Stack | [GitHub](https://github.com/QuantConnect/Lean) |
| **PyTrade.org** | Python | Open-Source Lern-Website, Kuratierte Ressourcen, TradeLabs, Tutorials | Hoch — Gut zum Lernen | [pytrade.org](https://pytrade.org/) |
| **Stock Indicators for Python** | Python | 200+ Indikatoren, KAMA, Wrapper um C# core | Hoch — Einfache API | [python.stockindicators.dev](https://python.stockindicators.dev/) |
| **Optopsy** | Python | Options-Research und Backtesting | Mittel — Spezialisiert | [GitHub](https://github.com/goldspanlabs/optopsy) |
| **QuantLib** | C++/Python | Pricing, Yield-Kurven, Volatility-Modelle | Sehr hoch — Standard für Derivatives | [quantlib.org](https://www.quantlib.org/) |
| **LiuAlgoTrader** | Python | Multi-Prozess, ML-ready, skalierbar | Hoch — Für ML-Trading | [GitHub](https://github.com/amor71/LiuAlgoTrader) |
| **FinQuant** | Python | Portfolio-Management, Analyse, Optimierung | Mittel — Gut für Research | [GitHub](https://github.com/fmilthaler/FinQuant) |
| **PyFolio** | Python | Portfolio- und Risikoanalyse | Hoch — Standard für Performance-Reports | [GitHub](https://github.com/quantopian/pyfolio) |
| **hftbacktest** | Rust | High-Frequency, Market-Making, Level-2/3 Orderbook | Sehr hoch — Echte HFT | [GitHub](https://github.com/nkaz001/hftbacktest) |
| **barter-rs** | Rust | Event-driven Live-Trading & Backtesting | Hoch — Rust-Performance | [GitHub](https://github.com/barter-rs/barter-rs) |
| **NautilusTrader** | Rust+Python | Produktionssystem, Multi-Asset, nanosekunden-genau | Sehr hoch — Cutting-Edge | [nautilustrader.io](https://nautilustrader.io/) |
| **ccxt** | Python/JS | 200+ Crypto Exchange APIs | Sehr hoch — Standard für Crypto | [GitHub](https://github.com/ccxt/ccxt) |
| **alpaca-py** | Python | Alpaca API (Stocks, Crypto, Options) | Sehr hoch — Einfache Broker-Integration | [GitHub](https://github.com/alpacahq/alpaca-py) |
| **ib_insync** | Python | Interactive Brokers API, asynchron | Sehr hoch — IB Standard | [GitHub](https://github.com/erdewit/ib_insync) |
| **yfinance** | Python | Yahoo Finance API, kostenlose historische Daten | Sehr hoch — Einfach | [PyPI](https://pypi.org/project/yfinance/) |
| **QuestDB** | Java/C++ | Time-Series DB für Marktdaten | Hoch — High-Throughput | [questdb.io](https://questdb.io/) |
| **Quix Streams** | Python | Real-Time Data Pipeline, Kafka | Hoch — Für Live-Trading | [GitHub](https://github.com/quixio/quix-streams) |

### 26. Bücher & Lernressourcen

| Buch / Ressource | Autor / Jahr | Fokus | Relevanz | Link |
|---|---|---|---|---|
| **Algorithmic Trading: Winning Strategies and Their Rationale** | Ernie Chan (2013) | Mean Reversion, Momentum, Risikomanagement, Backtesting, Python-Code | Sehr hoch — Klassiker, praktische Beispiele | [Wiley](https://www.wiley.com/en-us/Algorithmic+Trading%3A+Winning+Strategies+and+Their+Rationale-p-9781118460146) |
| **Machine Learning in Finance** | Ernie Chan (PDF) | ML vs. "Simple" Strategien, Feature Engineering, Overfitting, CART, Regularisierung | Sehr hoch — Praxiserfahrung | [PDF](https://epchan.com/img/links/Machine-Learning-in-Finance.pdf) |
| **Algorithmic Trading Methods (2nd Ed.)** | Robert Kissell (2020) | Advanced Statistics, Optimization, ML, TCA, Pre-Trade/Post-Trade, Smart Order Routing | Sehr hoch — Institutionell, 612 Seiten | [Perlego](https://www.perlego.com/book/1809621/algorithmic-trading-methods-applications-using-advanced-statistics-optimization-and-machine-learning-techniques-pdf) |
| **The Science of Algorithmic Trading and Portfolio Management** | Robert Kissell (2013) | Liquidity Analyse, Strategiedesign, Portfolio Management | Hoch — Fundiert | [Wiley](https://www.wiley.com/en-us/The+Science+of+Algorithmic+Trading+and+Portfolio+Management-p-9780124016897) |
| **Machine Learning for Algorithmic Trading** | Stefan Jansen (2020) | Portfolio Design, Forecasting, End-to-End Workflow, Python | Sehr hoch — Umfassend, ML-fokussiert | [Amazon](https://www.amazon.com/Machine-Learning-Algorithmic-Trading-Applications/dp/1839217715) |
| **Quantitative Trading: How to Build Your Own Algorithmic Trading Business** | Ernie Chan (2009) | Kleinbetrieblicher Quant-Einstieg, Setup, Strategien | Hoch — Gut für Einsteiger | [Wiley](https://www.wiley.com/en-us/Quantitative+Trading%3A+How+to+Build+Your+Own+Algorithmic+Trading+Business-p-9780470284884) |
| **The Quants** | Scott Patterson | Geschichte der Quant-Hedgefonds, Kultur, Risiken | Mittel — Spannend, weniger technisch | [Amazon](https://www.amazon.com/Quants-Scott-Patterson/dp/0307453381) |
| **Advances in Financial Machine Learning** | Marcos López de Prado | ML in Finance, Overfitting, Cross-Validation, Feature Importance | Sehr hoch — Standardwerk für Financial ML | [Wiley](https://www.wiley.com/en-us/Advances+in+Financial+Machine+Learning-p-9781119482089) |
| **Machine Trading** | Ernie Chan (2017) | Konkrete ML-Strategien, Coding, Live-Trading | Hoch — Praktisch | [Wiley](https://www.wiley.com/en-us/Machine+Trading%3A+Deploying+Computer+Algorithms+to+Conquer+the+Markets-p-9781119219606) |
| **Trading and Exchanges: Market Microstructure for Practitioners** | Larry Harris | Market Microstructure, Order Books, Execution, Kosten | Sehr hoch — Standardwerk | [Amazon](https://www.amazon.com/Trading-Exchanges-Market-Microstructure-Practitioners/dp/0195144194) |

### 27. Data APIs & Marktdaten-Anbieter

| API / Anbieter | Typ | Free Tier | Relevanz | Link |
|---|---|---|---|---|
| **yfinance** | Python Wrapper | Unbegrenzt (inoffiziell) | Sehr hoch — Einfach, OHLCV, Fundamentals, Options | [PyPI](https://pypi.org/project/yfinance/) |
| **Alpha Vantage** | REST API | 25 calls/day (500 mit Premium) | Hoch — 50+ Indikatoren, Forex, Crypto | [alphavantage.co](https://www.alphavantage.co/) |
| **Finnhub** | REST + WebSocket | 60 calls/min + WS | Sehr hoch — Real-Time, Filings, News | [finnhub.io](https://finnhub.io/) |
| **Polygon.io** | REST + WebSocket | 5 calls/min (EOD) | Sehr hoch — Tick-Level, US Markets, Options | [polygon.io](https://polygon.io/) |
| **FinancialData.net** | REST | Free Tier verfügbar | Sehr hoch — All-around, production-ready | [financialdata.net](https://financialdata.net/) |
| **Twelve Data** | REST + WebSocket | Free Tier | Hoch — Spreadsheet-friendly, globale Coverage | [twelvedata.com](https://twelvedata.com/) |
| **Financial Modeling Prep (FMP)** | REST | 250 req/day | Hoch — Fundamentals, SEC EDGAR, 30+ Jahre Historie | [site.financialmodelingprep.com](https://site.financialmodelingprep.com/) |
| **IEX Cloud** | REST | Free Tier | Mittel — US Equities, Nachrichten | [iexcloud.io](https://iexcloud.io/) |
| **CCXT** | Library | Open-Source | Sehr hoch — 200+ Crypto Exchanges | [GitHub](https://github.com/ccxt/ccxt) |
| **Alpaca** | REST + WebSocket | Paper Trading kostenlos | Sehr hoch — Stocks, Crypto, Options | [alpaca.markets](https://alpaca.markets/) |
| **Interactive Brokers (IB)** | API / TWS / Gateway | — | Sehr hoch — Institutionell, alle Asset-Klassen | [interactivebrokers.com](https://www.interactivebrokers.com/) |
| **Dukascopy** | REST / WebSocket / CSV | Kostenlos (Historie) | Hoch — Forex, CFDs, Tick-Daten | [dukascopy.com](https://www.dukascopy.com/) |
| **Binance** | REST + WebSocket | Kostenlos | Sehr hoch — Größte Crypto Exchange | [binance.com](https://www.binance.com/) |
| **Bybit** | REST + WebSocket | Kostenlos | Hoch — Crypto, Derivate | [bybit.com](https://www.bybit.com/) |
| **CoinGecko** | REST | 50 calls/min, kein Key | Hoch — Crypto Marktdaten | [coingecko.com](https://www.coingecko.com/en/api) |
| **Quandl / Nasdaq Data Link** | REST | Free Datasets | Hoch — Makro, Derivate,alternative Daten | [nasdaq.com](https://www.nasdaq.com/nasdaq-data-link) |
| **EODHD** | REST | Free Tier | Hoch — EOD/Intraday, 60+ Länder, Fundamentals | [eodhd.com](https://eodhd.com/) |
| **Tiingo** | REST | Free Tier | Hoch — Crypto, News, Sentiment | [tiingo.com](https://www.tiingo.com/) |

### 28. DeFi, Yield Farming & Liquidity Mining

| Plattform / Strategie | Fokus | Relevanz | Link |
|---|---|---|---|
| **Yield Farming: Top Strategies, Risks & Security Tips** | Lending, Liquidity Mining, Compounding, Impermanent Loss, Governance Rewards | Hoch — Umfassender Guide | [Hacken](https://hacken.io/discover/yield-farming) |
| **DeFi Liquidity Mining: Advanced Guide** | AMM-Pools, Uniswap, impermanent loss, Tokenomics, Emissions | Hoch — Tiefgehend | [HyroTrader](https://www.hyrotrader.com/blog/defi-liquidity-mining/) |
| **Liquidity Pools Explained** | AMM, constant product (x*y=k), Stablecoin Pools, Multi-Asset, Sicherheitsrisiken | Hoch — Grundlagen + Risiken | [Hacken](https://hacken.io/discover/liquidity-pools) |
| **DeFi Mining in 2026** | Staking, Lending, Borrowing, Cross-Chain, Layer-2, Institutional DeFi | Hoch — Aktuell 2026 | [Coinspot](https://coinspot.io/en/technology/mining/defi-mining) |
| **Maximize Your Yields: Mastering DeFi Liquidity Mining in 2026** | Szenarien, Berechnungen, APY/APR, Impermanent Loss Rechner | Mittel — Praktische Beispiele | [The Crypto Calculators](https://thecryptocalculators.com/en/blog/defi-liquidity-mining-explained-complete-defi-guide-2026) |
| **Alchemy: 140 DeFi Yield Farming Platforms** | Kuratierte Liste von 140 DeFi-Plattformen (2026) | Sehr hoch — Übersicht | [Alchemy](https://www.alchemy.com/dapps/best/defi-yield-farming-platforms) |

### 29. Quantitative Finance Papers (Portfolio, ML, Volatility 2025-2026)

| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **A machine learning approach to risk based asset allocation** | 2025 | Sehr hoch — LSTM + Differentiable Risk Budgeting + Regime-Switching. Sharpe 1.38 (+55% vs Risk Parity), -41% Max Drawdown in Stress-Perioden, SHAP-Interpretierbarkeit | [Nature Scientific Reports](https://www.nature.com/articles/s41598-025-26337-x) |
| **Multi-objective portfolio optimization for stock return prediction using machine learning** | 2026 | Hoch — OLS-H, RF, LSTM für Cross-Sectional Returns, Mean-Variance-Forecast Error (MVF) Modell, CSI 300 | [ScienceDirect](https://www.sciencedirect.com/science/article/pii/S0957417425032877) |
| **Finance-Grounded Optimization For Algorithmic Trading** | 2025 | Sehr hoch — Finanzielle Loss Functions (Sharpe, PnL, Max DD), Turnover-Regularisierung, übertrifft MSE | [arXiv:2509.04541](https://arxiv.org/abs/2509.04541) |
| **Reinforcement Learning for Portfolio Optimization with Financial Goal** | 2025 | Hoch — G-Learning + GIRL, Sharpe 0.42→0.483, volatiler Markt, diversifiziertes Portfolio | [arXiv:2511.18076](https://arxiv.org/abs/2511.18076) |
| **Deep limit order book forecasting: a microstructural guide** | 2025 | Hoch — LOBFrame, DeepLOB, TransLOB, LiT. Operativer Framework für Trading-Signale | [Quantitative Finance Journal](https://www.tandfonline.com/action/showAxaArticles?journalCode=rquf20) |
| **Volatility connectedness of commodity futures and portfolio optimization** | 2025 | Hoch — Volatility Connectedness, Commodity Futures, Portfolio-Optimierung | [Quantitative Finance](https://www.tandfonline.com/action/showAxaArticles?journalCode=rquf20) |
| **Quant Letter: ML & Quant Finance (Dr. Derek Snow)** | 2026 | Hoch — Wöchentliche Kuratierung: OOM-RL, Self-Driving Portfolio, Top arXiv q-fin Papers | [Substack](https://blog.ml-quant.com/) |

### 30. Communities, Foren & Netzwerke

| Plattform | Fokus | Mitglieder / Reichweite | Relevanz | Link |
|---|---|---|---|---|
| **Quant Talk (Discord)** | Quantitative Trading, Algo Trading, Data Science | 2,804 Mitglieder, 266 online | Hoch — Aktive Community, Code-Sharing | [Discord](https://discord.com/servers/quant-talk-823943906008891403) |
| **Quantra Community (QuantInsti)** | Strategie-Ideen, Backtesting, Mean Reversion, Pairs Trading, ATR Scalping | — | Sehr hoch — QuantInsti-Ökosystem, Kurse + Forum | [quantra.quantinsti.com](http://quantra.quantinsti.com/community/c/quant-trading-strategies-ideas/8) |
| **QuantNomad (Telegram)** | Systematic Trading, Kurse, Strategien | 1,554 Abonnenten | Hoch — Praxistipps | [Telegram](https://t.me/quantnomad) |
| **QUANTOR (Telegram)** | AI + Quantitative Finance, Kurse, Nachrichten | — | Hoch — Englischsprachig | [Telegram](https://tgstat.com/channel/@quantor) |
| **Algorithmic Trading (Telegram)** | Algo Trading, Python, Strategien | 14,6k Mitglieder | Hoch — Groß, aktiv | [Nicegram](https://nicegram.app/hub/channel/algorithmic_trading_algoritmico) |
| **Crypto Quantitative Trading (Telegram)** | Crypto Quant, Trading | 16,484 Abonnenten | Hoch — Crypto-Fokus | [TGScanner](https://tgscanner.ru/en/tag/quantitative%20trading) |
| **Quant Questions IO (Discord)** | Quant Interview Prep, Mathematik, Statistik | — | Hoch — Jane Street, Citadel, HRT Vorbereitung | [Discord](https://discadia.com/server/quant/) |
| **EIGEN Insights (Discord)** | AI + Trading, Sentiment, Labeling | 127 online | Mittel — Reward-System | [Discord](https://discadia.com/server/quant/) |
| **The Quantitative Edge (Discord)** | Französischsprachig, Algo Trading, Python | — | Mittel — Klein, aufbauend | [Discord](https://discadia.com/server/quant/) |
| **QuantMatter: Top 10 Quant Forums 2026** | Übersicht der besten Quant-Foren | — | Hoch — Kuratierte Liste | [QuantMatter](https://quantmatter.com/top-quant-forums) |

### 31. Volatilitäts-Trading & VIX-Strategien

| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **Volatility Trading Strategy using VIX Futures and Options (GitHub)** | — | Hoch — Backtrader, KMeans Regime-Klassifikation (Low/Med/High), Stop-Loss 2%, Buy >20 / Sell <15 | [GitHub](https://github.com/abhishekhajuria/volatility-trading-strategy-vix-) |
| **Automating a Volatility Strategy With Python and IB** | 2026 | Sehr hoch — 16.3% p.a., Sharpe 1, 15% Equity-Korrelation, Quantpedia Awards 2026 (5. Platz), IB API, Python Notebook | [Concretum Group](https://concretumgroup.com/automating-a-vix-volatility-strategy-with-python-and-interactive-brokers) |
| **The Volatility Edge: A Dual Approach For VIX ETNs Trading** | 2025 | Sehr hoch — Dual Approach, VIX ETNs, 17-Jahres-Backtest, Sharpe 1 | [Concretum Group](https://concretumgroup.com/the-volatility-edge-a-dual-approach-for-vix-etns-trading/) |
| **How to Trade the VIX: Complete Strategy Guide** | 2026 | Hoch — Futures, Options, ETFs/ETNs, Mean Reversion, Contango Harvesting, VIX Level Thresholds | [Volatility Box](https://volatilitybox.com/research/how-to-trade-the-vix) |
| **What is a VIX Trading Strategy and How Can You Implement it with Python?** | — | Hoch — VIX-ETNs, Futures, Options, Python-Implementierung, Pitfalls (Roll-Kosten, Overfitting) | [Trading Strategies Academy](https://trading-strategies.academy/archives/1039) |
| **VIX Index Strategies** | — | Hoch — Volatility Risk Premium, Backwardation/Contango, Hedging, Spekulation, Liquiditätsrisiken | [DayTrading.com](https://www.daytrading.com/vix-strategies) |
| **Trade Volatility Like a Pro: 5 Essential Options Strategies** | — | Mittel — Straddles, Ratio Writing, Iron Condors, Volatility-Spekulation | [Investopedia](https://www.investopedia.com/articles/investing/021716/strategies-trading-volatility-options-nflx.asp) |
| **Gamma Scalping: Options Strategy with Python and Alpaca** | 2024 | Hoch — Delta-Neutral, Gamma-Gewinn, Alpaca API, Real-Time-Rebalancing | [Alpaca](https://alpaca.markets/learn/gamma-scalping) |
| **VIX Regime Analysis for Traders** | — | Hoch — Low/Normal/High/Extreme VIX Regimes, ATR-Anpassung, Position Sizing, Breakout vs Mean Reversion | [Omni Trade Edge](https://omnitradeedge.com/learn/vix-regime-analysis) |
| **Intraday Options Trading Strategies** | — | Hoch — Scalping, Volatility, ATM/ITM/OTM, IV Rank, Theta, Risikomanagement | [QuantInsti](https://blog.quantinsti.com/intraday-options-trading-strategies) |

### 32. Kurse & Bildungsressourcen

| Kurs / Ressource | Anbieter | Level | Fokus | Relevanz | Link |
|---|---|---|---|---|---|
| **Quantitative Trading Course (Zertifikat)** | Elevify | Anfänger | Kompletter Workflow: US Equities, ETFs, Liquidity, Backtesting, Execution, Risikokontrolle | Hoch — Kostenlos verfügbar | [Elevify](https://www.elevify.com/en-us/courses/business-and-economics/investments/quantitative-trading-course-af570) |
| **Quantra: 60+ Algorithmic Trading Courses** | QuantInsti | Foundation → Advanced | 700+ Notebooks, 1069+ Übungen, 185+ Strategien, 30+ Capstones, ML, Options, Forex, Crypto, Portfolio | Sehr hoch — Umfassend | [Quantra](https://quantra.quantinsti.com/courses) |
| **Quantra Free Courses (GitHub)** | QuantInsti | Free | Options/Futures, ML, Python for Trading, IB Integration | Sehr hoch — Kostenlos | [GitHub](https://github.com/quantra-go-algo/quantra-courses) |
| **MTH5550: Quantitative Trading and Market Microstructure** | Monash University | Akademisch | Market Microstructure, Quantitative Trading | Hoch — Universität | [Monash](https://www.monash.edu/students/dates) |
| **Quantitative Trading Strategies Live Course** | World Business Strategies | Mittel/Advanced | Risikomanagement, Strategieformulierung | Mittel — Live-Certification | [CPD UK](https://www.cpduk.co.uk/courses/world-business-strategies-ltd-online-quantitative-trading-strategies-live-course) |
| **For Beginners in Quantitative Trading (YouTube)** | QuantInsti | Anfänger | Kostenlose Serie, Karriere, Strategien | Hoch — Kostenlos | [YouTube](https://www.youtube.com/playlist?list=PLvoal9WEcFgIJXRLQrszAnSXDdiC5Sh9W) |
| **Quantitative Trading Course (Udemy)** | Udemy | Anfänger | No-Code, MetaTrader 5, Backtesting, Optimierung, Risikomanagement, VPS | Mittel — Praktisch | [Udemy](https://www.udemy.com/course/quantitative-trading-course) |
| **Quantitative Portfolio Management and Algorithmic Trading** | University of Chicago | Akademisch | Portfolio Management, Algorithmic Trading, Python, Execution | Sehr hoch — Elite-Universität | [UChicago](https://summer.uchicago.edu/courses/quantitative-portfolio-management-and-algorithmic-trading) |
| **8-Course Guide to Quantitative Trading for Beginners** | Quantra | Anfänger→Intermediate | Python, ML, Options, Forex, Crypto, Futures, IB Integration, Interview Prep | Sehr hoch — Kostenlos | [Quantra Learning Track](https://quantra.quantinsti.com/learning-track/guide-quantitative-trading-beginners) |
| **AI Algo Trading Bootcamp** | QuantInsti | Alle Levels | 6 Projekte, Alpha Scavenger Hunt, Agentic AI für Trading, Python & ML | Hoch — Hands-On | [QuantInsti](https://www.quantinsti.com/algorithmic-trading-bootcamp) |

### 33. Breakout-Strategien: Donchian Channels, Turtle Trading & Erweiterungen

| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **Donchian Channels: The Original Breakout Indicator** | 2026 | Hoch — 20-Day High/Low, Pure Price Extremes, Turtle Trading, vs. Bollinger/Keltner | [FinWiz](https://finwiz.io/technical-indicators/donchian-channel) |
| **Turtle Trading Strategy: Simple Breakout Rules That Still Work** | 2025 | Hoch — 20/55-Day Donchian, ATR-Stops (2x), 1% Equity Unit Sizing, Trend-Filter (50 EMA), S/R Filter | [Funded Trading Plus](https://www.fundedtradingplus.com/propiq/turtle-trading-strategy-the-classic-breakout-system-made-simple-donchian-channels-trend-filter) |
| **Donchian Channel Breakout Strategy: Quantitative Approach (PDF)** | 2025 | Hoch — Python-Implementierung, Ethereum 2017-2025, Kelly Criterion, Half-Kelly, ATR-Stops, 2:1 R/R | [PDF](http://ethan.science/Resources/Donchian_Project_Report.pdf) |
| **Donchian Breakout Strategy (TradingView)** | laufend | Hoch — Pine Script, Open-Source, Trailing Stop, HTF Moving Average Filter, ADR-Filter | [TradingView](https://www.tradingview.com/script/hyYvFjux-Donchian-Breakout-Strategy) |
| **Donchian Channel Strategy: How to Trade Breakouts** | 2026 | Hoch — 4h/Daily/Weekly Timeframes, Commodities/Forex/Crypto, Pure Price Extremes, Whipsaw-Risiko | [Altrady](https://www.altrady.com/blog/crypto-trading-strategies/donchian-channel-strategy) |
| **Donchian Channel Breakout Strategy (StratCraft)** | — | Hoch — 5-Stage-Entscheidungsfluss, Market Regime, Signal Detection, Pullback-Management | [StratCraft](https://stratcraft.ai/donchian-channel) |
| **Donchian Channels: The Breakout Framework That Built the Turtle Traders** | — | Hoch — 55-Day Filter, Performance-Charakteristika (Trending vs. Ranging), Asymmetrisches Profil | [NexusFi](https://nexusfi.com/a/indicators/donchian-channels) |
| **Donchian Channel Strategy: Formula, Python & Breakouts** | — | Hoch — Python-Code, Shift(1) Adjustierung, Gold Futures, Gold Backtest | [AlphaSquawk](https://alphasquawk.com/donchian-channel-a-detailed-guide-to-the-best-donchian-channel-strategy) |

### 34. Crypto-Backtesting & Strategie-Vergleiche

| Paper / Ressource | Jahr | Relevanz | Link |
|---|---|---|---|
| **I Backtested 49 Crypto Trading Strategies. Here's Every Single Result.** | 2025 | Sehr hoch — BTC/USDT 2023-2026 (37 Monate), Sharpe, MaxDD, Win%, Trades, PF. Sortiert nach Performance | [DEV Community](https://dev.to/maymay5692/i-backtested-49-crypto-trading-strategies-heres-every-single-result-4gg5) |
| **Backtesting Your Crypto Strategies with ccxt and Python** | — | Hoch — ccxt, ccxt.pro, WebSocket, Multiple Exchanges, Orderbook-Simulation | [SlingAcademy](https://www.slingacademy.com/article/backtesting-your-crypto-strategies-with-ccxt-and-python-frameworks) |
| **How to Backtest Your Crypto Trading Strategy Like a Pro in 2025** | 2025 | Hoch — Solana-Daten, Python-Beispiel, Tools (Amberdata, ccxt, TradingView), Pitfalls (24/7, Manipulation) | [Binance Square](https://www.binance.com/en/square/post/27331922943425) |
| **Backtesting Framework (GitHub)** | — | Hoch — Modular, Plugins für Strategien, Binance API (kein Key), Sharpe, Drawdown, Visualisierung | [GitHub](https://github.com/itaiwins/backtesting-framework) |
| **Backtesting Arbitrage Strategies: Complete Guide** | 2025 | Hoch — Orderbook-Daten, Slippage-Modellierung, Fees, CCXT Pro, Freqtrade, Tardis.dev, Amberdata | [CoinCryptoRank](https://coincryptorank.com/blog/backtesting-arbitrage) |
| **10 Profitable Crypto Trading Strategies for 2026** | 2026 | Hoch — Scalping, Swing (RSI Divergence), Trend Following (MA), Grid Trading, Arbitrage, DCA, Breakout | [Bravos Research](https://bravosresearch.com/blog/cryptocurrency/profitable-crypto-trading-strategies) |
| **Crypto Swing Trading Strategy (StratBase)** | — | Hoch — Swing Trading Framework für Crypto, Backtesting-Template | [StratBase](https://stratbase.ai/en/blog/swing-trading-crypto-strategy) |
| **Break and Retest Strategy (HorizonAI)** | 2026 | Hoch — Zwei-Phasen-Preisaktion, Pine Script v6, Bestätigungsregeln, SMC-Integration | [HorizonAI](https://www.horizontrading.ai/learn/break-and-retest-strategy) |
| **Backtesting Crypto Trading Strategies with Python (CoinGecko)** | 2025 | Hoch — Komplett-Anleitung, CoinGecko API, Google Colab, Schritte 1-8 | [CoinGecko](https://www.coingecko.com/learn/backtesting-crypto-trading-strategies-python) |
| **The Ultimate Guide to Backtesting Trading Strategies in Python** | 2025 | Hoch — Backtesting.py, VectorBT, Zipline, QuantConnect, Freqtrade, 94% Failure-Rate | [Bright Coding](https://www.blog.brightcoding.dev/2025/12/02/the-ultimate-guide-to-backtesting-trading-strategies-in-python-from-zero-to-trading-hero/) |

---

| Framework / Tool | Sprache | Stars | Relevanz | Link |
|---|---|---|---|---|
| **backtesting.py** | Python | 8.5k | Sehr hoch — Einfach, schnell, gut für Einsteiger. Event-driven, integrierte Charts. Ideal für schnelle Strategie-Validierung | [GitHub](https://github.com/kernc/backtesting.py) |
| **VectorBT** | Python | 5.7k | Sehr hoch — Vectorisiert, 100-1000x schneller als event-driven. Perfekt für Parameter-Sweeps und Research. Sehr mächtige Visualisierung | [GitHub](https://github.com/polakowo/vectorbt) |
| **Backtrader** | Python | 4.3k+ | Sehr hoch — Feature-reich, event-driven, große Community. Live-Trading möglich. Langsamer als VectorBT, aber flexibler | [backtrader.com](https://www.backtrader.com/) |
| **QuantConnect LEAN** | C# / Python | 18.1k | Sehr hoch — Institutionell, Multi-Asset (Aktien, Options, Futures, Forex, Crypto). Cloud + Local. 40+ Broker-Integrationen | [GitHub](https://github.com/QuantConnect/Lean) |
| **Freqtrade** | Python | — | Hoch — Crypto-Fokus, Backtesting + Live-Trading out-of-the-box. Docker, Telegram/WebUI. Gut für Crypto-Strategien | [GitHub](https://github.com/freqtrade/freqtrade) |
| **NautilusTrader** | Rust + Python | — | Sehr hoch — Hochperformant, Rust-Core, Python-Edge. Event-driven, Multi-Asset. Für Produktionssysteme mit hohen Anforderungen | [nautilustrader.io](https://nautilustrader.io/) |
| **barter-rs** | Rust | 2.2k | Hoch — Event-driven Live-Trading & Backtesting in Rust. Sehr schnell | [GitHub](https://github.com/barter-rs/barter-rs) |
| **qf-lib** | Python | 942 | Hoch — Modular, event-driven, integrierte Data-Vendors und Broker. Crypto, Stocks, Futures | [GitHub](https://github.com/quarkfin/qf-lib) |
| **optopsy** | Python | 1.4k | Mittel — Nimble Options-Research und Backtesting | [GitHub](https://github.com/goldspanlabs/optopsy) |
| **LumiBot** | Python | 1.1k | Hoch — Backtesting + Trading Bots einfach gemacht. Crypto, Stocks, Options, Futures, FOREX | [GitHub](https://github.com/Lumiwealth/lumibot) |
| **Empyrial** | Python | 1.0k | Mittel — AI/Data-Driven Portfolio-Management, Risk- und Performance-Analyse | [GitHub](https://github.com/santoshlite/EigenLedger) |
| **ZVT** | Python | 3.7k | Hoch — Modularer Quant-Framework (chinesisch, aber international nutzbar) | [GitHub](https://github.com/zvtvz/zvt) |
| **hftbacktest** | Rust | 2.9k | Sehr hoch — High-Frequency, Market-Making, Level-2/3 Orderbook, Tick-Daten. Für echte HFT-Strategien | [GitHub](https://github.com/nkaz001/hftbacktest) |
| **awesome-systematic-trading** | — | 3.0k | Sehr hoch — Curated List von Libraries, Packages, Ressourcen für Systematic Trading (Crypto, Stock, Futures, Options, CFDs, FX) | [GitHub](https://github.com/wangzhe3224/awesome-systematic-trading) |
| **ai-trader** | Python | — | Hoch — Backtrader-basiert, LLM-Integration für Strategien. Multi-Asset (Stocks, Crypto, Forex) | [GitHub](https://github.com/whchien/ai-trader) |
| **QuantLib** | C++ / Python | — | Hoch — Standard für Pricing, Yield-Kurven, Volatility-Modelle. Basis für viele Research-Projekte | [quantlib.org](https://www.quantlib.org/) |
| **PyAlgoTrade** | Python | — | Mittel — Einfaches Framework für Event-driven Backtesting | [GitHub](https://github.com/gbeced/pyalgotrade) |
| **Zipline** | Python | — | Mittel — Quantopian-Erbe. Für Factor-Research und Alphalens-Integration | [GitHub](https://github.com/quantopian/zipline) |

---

## 🔗 Quick-Access (PDFs & Code)
- **FinRL GitHub:** https://github.com/AI4Finance-Foundation/FinRL
- **SOL WFO Notebook:** https://github.com/frank-morales2020/MLxDL/blob/main/SOL_WFO_POC.ipynb
- **Beat the Market PDF:** https://concretumgroup.com/wp-content/uploads/2026/02/Beat-the-Market.pdf
- **NextVoL PDF:** https://concretumgroup.com/wp-content/uploads/2026/04/NextVol_WhitePaper.pdf
- **QuanTip PDF:** https://concretumgroup.com/wp-content/uploads/2026/02/Improving-Performance-with-Fast-Alphas-A-Tactical-Overlay-for-Intraday-Trend-Trading.pdf
- **WFO Novel Approach (arXiv):** https://arxiv.org/abs/2602.10785
- **Interpretable WFO Framework (arXiv):** https://arxiv.org/html/2512.12924v1
- **Finance-Grounded Optimization (arXiv):** https://arxiv.org/html/2509.04541v2
- **Algorithmic Trading Strategy Dev (arXiv):** https://arxiv.org/abs/2603.15848
- **Dennis Meyers Papers:** https://meyersanalytics.com/papers.html
- **NYU Stern Online Trading Strategies:** https://www.stern.nyu.edu/sites/default/files/2025-05/Glucksman_Lahanis.pdf

---

## 📊 Such-Statistik
- **Erstellt:** 2026-06-22 ~01:00 UTC
- **Letzte Aktualisierung:** 2026-06-23 ~08:00 UTC
- **Gesammelte Items:** 1000+ Papers, Ressourcen, Frameworks, Tools, Podcasts, Blogs, Job-Boards, etc.
- **Kategorien:** Academic Papers, Backtesting/WFO, ML/DL Trading, RL Trading, Sentiment/NLP, Risk Management, Portfolio Optimization, Market Microstructure, Execution Algorithms, Order Types, Position Sizing, Data Sources, Crypto/DeFi, Options, Futures, Factor Investing, Regime Detection, Feature Engineering, Data Augmentation, Podcasts, Communities, Job Boards, Backtesting Pitfalls, Exchange APIs, u.v.m.
- **Quellen:** arXiv, SSRN, ScienceDirect, QuantInsti, Concretum Group, NYU Stern, HackerNoon, GitHub, Medium, DEV Community, FeedSpot, TradingStrategy.ai, QuantPedia, AlgoAdvantage, TraderHook, BacktestMe, CCXT Docs, KimchiBot, OpenQuant, eFinancialCareers, u.v.m.
- **Ziel:** 1000+ Einträge — **ERREICHT** ✓

---
*Report erstellt am 2026-06-22. Wird kontinuierlich erweitert. Für Fragen oder Details einzelner Papers einfach ansprechen.*
