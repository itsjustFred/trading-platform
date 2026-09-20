# Strategy-Fixes-Audit — Geparkte Findings (egal / später)

- **Datum (UTC):** 2026-09-15
- **Quelle:** `/home/ubuntu/tmp/big-findings-audit.md` (44 Findings aus 4 Reviews: GLM 5.3, GLM 5.3 Flash, Muse Spark 1.3 Contributor, GPT-5.6 Luna; Kimi K3 ohne Beitrag)
- **Geprüft gegen:** Worktree `/home/ubuntu/worktrees/strategy-fixes` @ `5dd883b`
- **Lesart:** Diese Datei parkt bewusst alles, was NICHT in die Integrations-PR kommt. Must-fix (9) + Should-fix (~20) laufen über PR + GH-Issue (Triage). Jedes Item unten nennt Ort, Befund, Park-Grund und Fix-Idee für eine spätere Hygiene-PR.

---

## 1. Latent — Mechanismus real, heute ungefährlich

### A-H6 — `signal_exit_fill="open"` ohne Guard (niedrig)
- **Ort:** `runner/engines/base.py:94-98`, `runner/engine_pkg/trading_engine.py:559-587`
- **Was:** Exit-Signal aus Daten bis inkl. Bar-Close i füllt am Open DESSELBEN Bars i — bei close-basierten Exits wäre das ein Retro-Fill. Heute nutzen nur IVB-Time-Exits (`minute >= 22:55`, preisunabhängig) das Flag → kein aktueller PnL-Bias.
- **Park-Grund:** kein Verhaltenseffekt heute; Guard/Warnung reicht, keine Eile.
- **Fix-Idee:** Warnung/Guard, wenn ein preisabhängiger Exit unter dem Flag läuft.

## 2. Stats-Hygiene — falsche Kennzahlen in Nebenpfaden

### A-H8 — Risk-Parity-`lean_statistics` ohne LEAN-Semantik (niedrig)
- **Ort:** `scripts/run_risk_parity_backtest.py:273-296`
- **Was:** Tagesmittel annualisiert, skalare Jahres-`risk_free_rate` subtrahiert, Sortino via `downside.std()` — statt Excess-Returns pro Periode und Downside-Deviation gegen Target.
- **Park-Grund:** wirkt nur bei `lean_statistics=True` + rf≠0; Standard-Runs unbetroffen.
- **Fix-Idee:** per-period Excess-Returns, Downside-Deviation gegen rf/Target.

### A-M5 — Sharpe Pop- vs. Stichprobenvarianz (Kosmetik)
- **Ort:** `runner/stats.py:467-478`
- **Was:** manueller Sharpe teilt durch n, QS-Pfad nutzt ddof=1 → systematisch `sqrt((n-1)/n)` auseinander.
- **Park-Grund:** kein PnL-Effekt; 1-Zeilen-Fix (`ddof=1`), Quick Win für Hygiene-PR.

### A-L2 — Sortino Target-0 vs. QS rf-Target (niedrig)
- **Ort:** `runner/stats.py:481-495` vs `:157-162`
- **Was:** zwei Sortino-Definitionen; pre-existing, nicht im Diff.
- **Park-Grund:** kein Diff-Bezug, kein Bias im Hauptpfad.

## 3. Edge-Cases — selten, klein

### A-M12 — Nativer Fixed-Risk-Pfad verletzt `min_lot` nach Cap (Edge-Case)
- **Ort:** `runner/engine_pkg/trading_engine.py:243-256`
- **Was:** `shares = max(shares, min_lot)` läuft VOR dem Leverage-Cap; danach keine Re-Normalisierung.
- **Park-Grund:** nur wenn der Cap exakt unter das Minimum schneidet.
- **Fix-Idee:** nach dem Cap erneut auf Lot-Step/`min_lot` normalisieren oder Trade droppen.

### A-L8 — `_extract_levels` sortiert TP-Namen lexikographisch (vernachlässigbar)
- **Ort:** `runner/engine_pkg/trading_engine.py:157-181`
- **Was:** `"tp10" < "tp2"` als String-Sort.
- **Park-Grund:** erst ab 10 Levels relevant, aktuell 1–2.

## 4. Dead Code — kein Runtime-Effekt

### A-M10 — Toter `_build_vectorbt_trade` mit alter Leverage-Semantik (Kosmetik)
- **Ort:** `runner/engines/vectorbt_adapter.py:486-601`
- **Was:** kein Aufrufer mehr; konserviert `max_leverage < max_position_pct → floor` gegen die gefixte Independent-Cap-Semantik.
- **Park-Grund:** kein Verhalten heute; nur Reaktivierungs-Falle. Löschen ist trivial → Quick Win für Hygiene-PR.

## 5. Strategie-Doku / Approximation — dokumentiert oder theoretisch

### B-M3 — channel_breakout Gap-Filter ohne `timestamp`-Spalte aus (niedrig)
- **Ort:** `strategies/channel_breakout/channel_breakout.py:63-100, 121-163, 297-304`
- **Was:** Bypass ist dokumentiert; der NaT-Key-Anteil (Luna) ist aus dem Code nicht belegbar, rein theoretisch.
- **Park-Grund:** kein belegter Bug, nur Doku-Status.

### B-L3 — post_vix 50%-Scale-Out als VOLLER Exit (niedrig)
- **Ort:** `strategies/post_vix_dip_buy/post_vix_dip_buy.py:346-352`
- **Was:** im Reason-Namen dokumentierte Approximation, verändert Economics systematisch.
- **Park-Grund:** bewusste, deklarierte Vereinfachung — kein stiller Bug.

### B-L5 — MT5-Parser hängt an absolutem Host-Pfad (vernachlässigbar)
- **Ort:** `scripts/parse_mt5_reference.py:30` (`/home/ubuntu/trading-platform-references`)
- **Was:** nur Audit-Tooling, keine Pipeline-Wirkung.
- **Park-Grund:** kein Produktcode.

### B-L2 — post_vix `near_support` am Vortagestief (unentscheidbar)
- **Ort:** `strategies/post_vix_dip_buy/post_vix_dip_buy.py:184-188, 289`
- **Was:** Code bestätigt (Vortagestief via `shift(1)`), aber ohne Pine-Source-Text keine Fidelity-Aussage möglich.
- **Park-Grund:** erst Pine-Quelle prüfen, dann entscheiden.

## 6. Falsch-positiv — kein Fix, nur zur Akte

### A-L7 — `QC_DATA_START`-Vergleich bei tz-awarem Index
- **Ort:** `scripts/run_risk_parity_backtest.py:429-432`
- **Was:** Behaupteter `TypeError` tritt nicht auf — `download_prices` liefert tz-naiven Index; tz-awares Feed würde laut crashen, nicht still.

### B-L4 — us100 GMT+3 ↔ NY nur während EDT stimmig
- **Ort:** `strategies/us100_scalper/us100_scalper.py:13-15`
- **Was:** Strategie rechnet DST-korrekt in `America/New_York`; GMT+3 steht nur im Quellen-Kommentar. Winter-Shift rein hypothetisch.

---

## Abgrenzung zur PR

In die Integrations-PR (Branch `strategy-fixes` + GH-Issue-Triage) gehören nur **must-fix (9)** und **should-fix (~20)**. Diese Datei ist das Gegenstück: alles Geparkte mit Begründung, damit nichts verloren geht und niemand es versehentlich in die PR zieht.
