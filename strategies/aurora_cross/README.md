# Aurora Cross

## Quelle
TradingView Pine Script v5 (Originalbezeichnung: "Impulse Cross")

## Kernlogik
Trendfolgestrategie basierend auf der Kreuzung zweier exponentieller Gleitender Durchschnitte (EMA). Generiert Long-Signale bei bullisher Kreuzung (schneller EMA über langsamen EMA) und schließt Positionen bei bärischer Gegenkreuzung. Die Strategie operiert ausschließlich auf Basis der Indikator-Signale ohne zusätzliche Filter oder volatilitätsbasierte Ausstiege.

### Entry
**Long Entry**: Ausführung bei `ta.crossover(fast, slow)`
- Bedingung: 9-Perioden-EMA kreuzt von unten nach oben über den 21-Perioden-EMA
- Ausführung: Marktorder zum Schlusskurs der Signal-Kerze
- Label: "Long"

### Exit
**Vollständige Positionsschließung**: Ausführung bei `ta.crossunder(fast, slow)`
- Bedingung: 9-Perioden-EMA kreuzt von oben nach unten unter den 21-Perioden-EMA
- Ausführung: `strategy.close("Long")` - Schließung zum Schlusskurs der Ausstiegs-Kerze
- Keine Partial Exits implementiert

### Stop-Loss
Nicht explizit definiert. Die Strategie verwendet ausschließlich den bärischen EMA-Crossunder als einzigen Ausstiegsmechanismus (Signal-Exit). Es besteht keine technische Begrenzung des maximalen Verlusts pro Trade über ein fixiertes Stop-Level hinaus.

## Indikator-Einstellungen
| Indikator | Parameter | Berechnung |
|-----------|-----------|------------|
| Fast EMA | 9 Perioden | `ta.ema(close, 9)` |
| Slow EMA | 21 Perioden | `ta.ema(close, 21)` |
| Preisdaten | Schlusskurs | `close` |

## Risk-Management
- **Positionsgröße**: Nicht spezifiziert (Standard: 100% Equity pro Trade)
- **Take-Profit**: Nicht implementiert
- **Stop-Loss**: Nicht implementiert
- **Trailing Stop**: Nicht implementiert
- **Max Drawdown Protection**: Nicht vorhanden
- **Risk-Reward-Ratio**: Keine Definition
- **Exposure**: Dauerhachte Marktexposition zwischen Entry- und Exit-Signal (keine Flat-Phasen außerhalb der Kreuzungspunkte)

## Framework-Empfehlung
**vectorbt**  
Begründung: Die einfache EMA-Crossover-Logik lässt sich in vectorbt effizient vektorisiert implementieren, was schnelle Backtests auf großen Datenmengen ermöglicht. Das Framework unterstützt native EMA-Berechnungen und Signal-basierte Entries/Exits ohne Notwendigkeit komplexer Event-Handler, was die Migration vom Pine Script-Original vereinfacht.
