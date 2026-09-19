# Tiingo News Sentiment Long-Short — BLOCKED SKELETON

> **Status: BLOCKED, nicht implementiert.** Dieses Verzeichnis enthaelt nur ein
> Skelett (`STRATEGY_META` + `generate_signals` wirft `NotImplementedError`).
> Es gibt in diesem Repo keinen News-Feed, und offline kann keiner gebaut werden.

## Quelle
- Original: `tiingo_sentiment_long_short` (`algorithm.py`) aus `alexisbouley2/quantconnect`
  (`https://github.com/alexisbouley2/quantconnect/tree/main/strategies/tiingo_sentiment_long_short`)
- Erfasst: 2026-09-09 (als blockiert gemeldet, nicht gefixt)

## Source-Logik (was implementiert werden muesste)
1. **News-Erfassung:** pro Symbol Tiingo-Artikel der letzten `news_days = 7`
   Handelstage (`add_data(TiingoNews, symbol)` + `history[TiingoNews]`).
2. **Scoring:** Keyword-Lexikon (`word_scores`, z. B. `good +0.5` / `bad -0.5`)
   ueber die Wortliste jeder Artikel-Description summiert.
3. **Aggregation:** exponentielle Recency-Gewichtung (`exp(linspace(0, 1, n))`,
   normiert) ueber die Artikel-Scores eines Symbols.
4. **Ranking:** taeglich 30 Min. nach Eroeffnung Sortierung nach Sentiment;
   long die Top `max_positions/2 = 5`, short die Bottom 5 (marktneutral),
   Gleichgewichtung mit `cash_margin = 3 %` Cash-Reserve.
5. **Rebalance:** Positionen, die aus Top/Bottom fallen oder die Richtung
   wechseln, werden liquidiert.

## Benoetigter Daten-Adapter (warum blockiert)
- Ein News-Vendor-Dataset im QuantConnect-Sinn: Artikel-Descriptions pro Symbol
  und Tag (`TiingoNews`-Aequivalent), plus `history`-Zugriff mit 7-Tage-Lookback
  und 30-Tage-Warmup.
- Im Repo existiert kein News-Feed (nur OHLCV-Downloader: Dukascopy/yfinance/
  Alpaca etc.) und offline laesst sich keiner bauen.
- Sentiment aus Preisdaten zu "faken" (z. B. Momentum als Sentiment-Proxy)
  wuerde die Source-Logik falsch darstellen — deshalb wirft `generate_signals`
  `NotImplementedError` mit dieser Begruendung, statt stille Fake-Signale zu liefern.

## Indikator-Einstellungen (Quelle)
| Parameter | Wert |
|-----------|------|
| Max Positionen | 10 (5 long / 5 short) |
| News-Lookback | 7 Handelstage |
| Cash-Margin | 0.03 |
| Rebalance | taeglich, 30 Min. nach Eroeffnung |
| Warmup | 30 Tage |

## Risk-Management
Entfaellt (blockiert). Quelle: marktneutrale Gleichgewichtung long/short.

## Framework-Empfehlung
Noch nicht lauffaehig. Nach Entsperrung: vectorbt oder native Engine mit
taeglichem Rebalance auf Tagesbasis (kein Intraday-Anteil in der Quelle).
