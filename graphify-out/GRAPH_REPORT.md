# Graph Report - trading-platform (2026-04-28)

## Corpus Check
- Large corpus: 311 files · ~764,118 words. Semantic extraction will be expensive (many Claude tokens). Consider running on a subfolder, or use --no-semantic to run AST-only.

## Summary
- 15 nodes · 6 edges · 9 communities detected
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Core API & Models|Core API & Models]]
- [[_COMMUNITY_Risk Management|Risk Management]]
- [[_COMMUNITY_Live Trading|Live Trading]]
- [[_COMMUNITY_Backtesting Engine|Backtesting Engine]]
- [[_COMMUNITY_Frontend Integration|Frontend Integration]]
- [[_COMMUNITY_Signal Processing|Signal Processing]]
- [[_COMMUNITY_Frontend Stack|Frontend Stack]]
- [[_COMMUNITY_Documentation|Documentation]]
- [[_COMMUNITY_Dependencies|Dependencies]]

## God Nodes (most connected - your core abstractions)
1. `Frontend Package` - 2 edges
2. `Viewer API` - 1 edges
3. `Shared Models` - 1 edges
4. `RiskEngine` - 1 edges
5. `TradeSignal` - 1 edges
6. `TradeEngine` - 1 edges
7. `LiveRunnerDaemon` - 1 edges
8. `API Client` - 1 edges
9. `App Router` - 1 edges
10. `React` - 1 edges

## Surprising Connections (you probably didn't know these)
- `Viewer API` --references--> `Shared Models`  [EXTRACTED]
  viewer/main.py → shared/models.py
- `TradeEngine` --calls--> `LiveRunnerDaemon`  [EXTRACTED]
  runner/engine.py → live_runner/daemon.py
- `RiskEngine` --references--> `TradeSignal`  [EXTRACTED]
  risk/engine.py → risk/models.py
- `App Router` --references--> `API Client`  [EXTRACTED]
  frontend/src/App.tsx → frontend/src/api/client.ts

## Communities

### Community 0 - "Core API & Models"
Cohesion: 0.67
Nodes (3): React, TanStack Query, Frontend Package

### Community 1 - "Risk Management"
Cohesion: 1.0
Nodes (2): Shared Models, Viewer API

### Community 2 - "Live Trading"
Cohesion: 1.0
Nodes (2): RiskEngine, TradeSignal

### Community 3 - "Backtesting Engine"
Cohesion: 1.0
Nodes (2): LiveRunnerDaemon, TradeEngine

### Community 4 - "Frontend Integration"
Cohesion: 1.0
Nodes (2): API Client, App Router

### Community 5 - "Signal Processing"
Cohesion: 1.0
Nodes (1): Trading Platform

### Community 6 - "Frontend Stack"
Cohesion: 1.0
Nodes (1): normalize_timeframe

### Community 7 - "Documentation"
Cohesion: 1.0
Nodes (1): Deployment Docs

### Community 8 - "Dependencies"
Cohesion: 1.0
Nodes (1): Signal Bots Docs

## Knowledge Gaps
- **14 isolated node(s):** `Trading Platform`, `Viewer API`, `Shared Models`, `RiskEngine`, `TradeSignal` (+9 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Risk Management`** (2 nodes): `Shared Models`, `Viewer API`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Live Trading`** (2 nodes): `RiskEngine`, `TradeSignal`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Backtesting Engine`** (2 nodes): `LiveRunnerDaemon`, `TradeEngine`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Frontend Integration`** (2 nodes): `API Client`, `App Router`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Signal Processing`** (1 nodes): `Trading Platform`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Frontend Stack`** (1 nodes): `normalize_timeframe`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Documentation`** (1 nodes): `Deployment Docs`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Dependencies`** (1 nodes): `Signal Bots Docs`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **What connects `Trading Platform`, `Viewer API`, `Shared Models` to the rest of the system?**
  _14 weakly-connected nodes found - possible documentation gaps or missing edges._