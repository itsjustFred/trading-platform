# trading-platform

Universal backtesting framework — run strategies across multiple engines (LEAN, vectorbt) and view results in a unified trade viewer.

## Quick Start

```bash
# Install (core only)
pip install -e .

# Install with vectorbt engine
pip install -e ".[vectorbt]"

# Install with viewer
pip install -e ".[viewer]"

# Install everything
pip install -e ".[all]"
```

## Structure

```
runner/       — Backtest runner CLI (python -m runner)
viewer/       — Trade viewer web app (python -m viewer)
data_tools/   — Data management (migration, catalog, downloaders)
shared/       — Shared models and constants
strategies/   — Strategy collection (for vectorbt/standalone)
tests/        — Test suite
```

## Documentation

- [Trade Format Spec](../server_planning/02_phase1_plan.md#1-trade-output-format-spezifikation)
- [Data Directory Structure](../server_planning/02_phase1_plan.md#2-data-verzeichnis-struktur)
