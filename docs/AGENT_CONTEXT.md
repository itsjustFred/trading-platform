# AGENT CONTEXT - Trading System Project (Repo-Aligned)

> This document is the single context file for coding agents in this repository.
> Read fully before making changes.

---

## 1) Project Goal

This repository is a custom algorithmic trading platform built end-to-end:

- strategy execution and backtesting
- unified result format
- browser viewer for analysis
- automated strategy conversion workflow

The design goal is local control, reproducibility, and deterministic behavior where possible.

---

## 2) Current State vs Target State (Important)

### Current state in this repo (today)

- Viewer backend is Flask in `viewer/app.py`.
- Frontend is React + Vite in `frontend/`.
- Primary runner path is `runner/cli.py`.
- Primary adapter path is `runner/engines/vectorbt_adapter.py`.
- Auto-conversion currently uses staged LLM flow in `runner/auto_agent.py` and writes strategy code directly.
- There is currently no `runner/strategy_ir/` directory checked in.

### Target state (in progress)

- Strategy conversion should move to an IR-first pipeline:
  - source strategy -> intent extraction -> StrategyIR -> deterministic codegen -> executable strategy
- LLM should focus on intent extraction and structured mapping, not final free-form strategy code generation.

If there is conflict between this document and actual code, code is source of truth.

---

## 3) System Layers

### Layer A: Viewer (Web App)

- Backend: Flask (`viewer/app.py`)
- Frontend: React (`frontend/src`)
- Purpose: inspect backtests, trades, equity, strategy artifacts, and conversion outputs.

### Layer B: Runner and Engines

- Entry: `runner/cli.py`
- Engines: `runner/engines/`
  - `vectorbt_adapter.py`
  - `pynecore_adapter.py`
  - `lean_adapter.py`
- Purpose: run strategies over OHLCV data and output universal backtest JSON.

### Layer C: Strategy Assets

- Location: `strategies/<slug>/`
- Typical files:
  - `<slug>.py`
  - `README.md`
  - optional helper/context files

### Layer D: Conversion Worker

- Current implementation: `runner/auto_agent.py`
- Purpose: convert raw strategy input into repository strategy assets and validate via backtest.

### Layer E: Planned IR Pipeline (migration path)

- Planned location: `runner/strategy_ir/`
- Planned components:
  - `models.py`
  - `codegen_vectorbt.py`
  - `drawing_mapper.py`
- Purpose: deterministic, testable strategy generation.

---

## 4) Canonical Runtime Flow (Current)

```text
strategy source
  -> runner/cli.py
  -> engine adapter
  -> shared.models.BacktestResult
  -> $TRADING_DATA_DIR/backtests/*.trades.json
  -> viewer/app.py
  -> frontend charts and tables
```

---

## 5) Strategy Contract (Current)

Strategies are expected to expose:

```python
def generate_signals(df: pd.DataFrame, params: dict):
    ...
```

Preferred outputs:

- `(entries, exits, indicators_dict)`
- `(entries, exits, indicators_dict, drawings)`

General rules:

- no global mutable runtime state
- deterministic behavior for same inputs
- use pandas/pandas_ta style operations
- keep output aligned with viewer semantics

---

## 6) IR-First Target Design

Use this as the desired design direction for migration tasks.

```python
@dataclass
class StrategyIR:
    name: str
    indicators: list[IndicatorRef]
    entry_conditions: list[Condition]
    exit_signals: list[ExitSignal]
    drawings: list[Drawing]
    parameters: list[Parameter]
    state_machine: StateMachine | None
    raw_expressions: list[RawExpression]
    confidence: float
    fallback_code: str
```

Intent:

- deterministic mapping from IR to executable strategy code
- preserve drawing fidelity from strategy logic to viewer payload
- support escape hatch via `RawExpression` where schema is not yet expressive enough

---

## 7) Hard Rules For Agents

- Do not add external broker/trading API integrations unless explicitly requested.
- Do not introduce new dependencies unless required and approved.
- Prefer existing project stack and conventions over new abstractions.
- For conversion work, do not expand direct free-form codegen if the task is IR migration.
- Keep changes scoped: fix what the task asks, avoid unrelated refactors.

---

## 8) Task Routing Guide

- Viewer/API behavior or payload issues:
  - `viewer/` and `frontend/src/api/`
- Backtest execution issues:
  - `runner/cli.py` and `runner/engines/`
- Strategy-specific logic:
  - `strategies/<slug>/`
- IR migration tasks:
  - create/extend `runner/strategy_ir/` and integrate through `runner/auto_agent.py`

---

## 9) IR Task Definition Of Done (DoD)

For tasks that touch IR pipeline design or implementation, aim to satisfy all:

1. IR schema change is explicit and documented.
2. Deterministic codegen behavior is preserved for same IR input.
3. `RawExpression` usage is constrained and justified.
4. Viewer drawing semantics remain consistent with strategy intent.
5. Tests are added/updated for parser, codegen, and at least one end-to-end conversion path.
6. Fallback behavior is explicit when IR confidence is low.

---

## 10) Setup and Runbook (Corrected)

```bash
# clone
git clone <repo-url>
cd trading-platform

# venv
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

# deps
pip install -e ".[all]"

# env
cp .env.example .env
# set TRADING_DATA_DIR if needed

# frontend build
cd frontend
npm install
npm run build
cd ..

# viewer start (current repo)
python -m viewer.app --port 5556
```

Notes:

- The viewer command is `python -m viewer.app` in this repository.
- `uvicorn viewer.main:app` is not the active runtime path in current code.

---

## 11) Practical Priorities

Current pragmatic priority order:

1. Keep existing runner + viewer workflows stable.
2. Introduce IR pipeline incrementally without breaking current conversion flow.
3. Preserve strategy-to-viewer fidelity, especially drawings and signal semantics.
4. Add tests with each migration step.

---

This file is intentionally strict on architecture boundaries so autonomous agents can make reliable, low-risk changes.
