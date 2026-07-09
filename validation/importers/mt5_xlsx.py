"""MT5 Strategy Tester XLSX importer."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from shared.models import Trade, TradeExit
from validation.importers.base import BaseImporter
from validation.reference import ValidationReference

logger = logging.getLogger(__name__)

# German labels we scan for when locating tables and parsing metadata.
_ORDERS_LABEL = "Orders"
_TRADES_LABEL = "Trades"

# Known German stat labels → canonical keys (with and without trailing colon)
_STAT_LABELS = [
    ("Gesamtanzahl Trades", "total_trades"),
    ("Gewonnene Trades (in % von Gesamt)", "winners_with_pct"),
    ("Verlorene Trades (in % von Gesamt)", "losers_with_pct"),
    ("Profitfaktor", "profit_factor"),
    ("Sharpe-Ratio", "sharpe_ratio"),
    ("Nettogewinn gesamt", "total_pnl"),
    ("Rückgang Kontostand relativ", "max_drawdown_pct"),
    ("Rückgang Kontostand absolut", "max_drawdown_abs"),
    ("Rückgang Kontostand maximal", "max_drawdown_max"),
    ("Rückgang Equity relativ", "max_drawdown_equity_pct"),
    ("Rückgang Equity maximal", "max_drawdown_equity_max"),
    ("Durchschnitt Gewinntrade", "avg_winner"),
    ("Durchschnitt Verlusttrade", "avg_loser"),
    ("Maximum Gewinntrades in Folge", "max_consecutive_wins"),
    ("Maximum Gewinntrades in Folge (Gewinn in", "max_consecutive_wins"),
    ("Maximum Verlusttrades in Folge", "max_consecutive_losses"),
    ("Maximum Verlusttrades in Folge (Verlust ", "max_consecutive_losses"),
    ("Bruttogewinn", "gross_profit"),
    ("Bruttoverlust", "gross_loss"),
    ("Erholungsfaktor", "recovery_factor"),
    ("Erwartetes Ergebnis", "expected_payoff"),
    ("AHPR", "ahpr"),
    ("GHPR", "ghpr"),
    ("Korrelation (Profit,MFE)", "corr_profit_mfe"),
    ("Korrelation (Profit,MAE)", "corr_profit_mae"),
    ("LR Korrelation", "lr_correlation"),
    ("LR Standardfehler", "lr_std_error"),
    ("Minimale Positionshaltedauer", "min_holding_time"),
    ("Maximale Positionshaltedauer", "max_holding_time"),
    ("Sell-Positionen (davon gewonnen %)", "sell_positions"),
    ("Buy-Positionen (davon gewonnen %)", "buy_positions"),
    ("Anzahl Deals", "total_deals"),
    ("Größter Gewinntrade", "max_winner"),
    ("Größter Verlusttrade", "max_loser"),
    ("Durchschnitt Gewinntrades in Folge", "avg_consecutive_wins"),
    ("Durchschnitt Verlusttrades in Folge", "avg_consecutive_losses"),
]

# Build a lookup that strips trailing colons and whitespace
_STAT_LABEL_MAP: dict[str, str] = {}
for label, key in _STAT_LABELS:
    _STAT_LABEL_MAP[label.rstrip(":")] = key
    _STAT_LABEL_MAP[label] = key
    _STAT_LABEL_MAP[label.rstrip(" :")] = key


class MT5XLSXImporter(BaseImporter):
    """Import MT5 Strategy Tester XLSX reports (German UI)."""

    def load(self, source: str | Path, source_timezone: str | None = None, **kwargs: Any) -> ValidationReference:
        """Read an MT5 XLSX report and return a ``ValidationReference``.

        Parameters
        ----------
        source:
            Path to the ``.xlsx`` file.
        source_timezone:
            Optional IANA timezone name (e.g. "Europe/Athens") of the report's timestamps.
            If provided, naive timestamps will be interpreted in this timezone and
            converted to UTC. Defaults to UTC if not provided.
        **kwargs:
            Forwarded to ``pandas.read_excel`` (e.g. ``engine``).
        """
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"MT5 XLSX report not found: {path}")

        df_raw = pd.read_excel(path, header=None, sheet_name=0, **kwargs)
        rows = df_raw.values.tolist()

        # Locate section headers dynamically
        orders_header_idx = self._find_row_with_cell(rows, _ORDERS_LABEL)
        trades_header_idx = self._find_row_with_cell(rows, _TRADES_LABEL)

        if orders_header_idx is None:
            raise ValueError("Could not locate 'Orders' section in the report.")
        if trades_header_idx is None:
            raise ValueError("Could not locate 'Trades' section in the report.")

        # Parse meta, settings, stats from header area
        meta = self._parse_meta(rows, orders_header_idx)
        settings = meta.pop("settings", {})
        stats = self._parse_stats(rows, orders_header_idx)
        raw_metadata = {"parsed_meta": meta, "parsed_stats": stats}

        # Parse Orders table for SL/TP from Orders table
        orders_df = self._extract_table(df_raw, orders_header_idx, end_idx=trades_header_idx)
        sltp_map = self._build_sltp_map(orders_df)

        # Parse Trades / Deals table
        trades_df = self._extract_table(df_raw, trades_header_idx, end_idx=None)
        trades = self._build_trades(trades_df, sltp_map, source_timezone)

        return ValidationReference(
            strategy_name=meta.get("strategy_name", "unknown"),
            source_framework="mt5_xlsx",
            symbol=meta.get("symbol", ""),
            timeframe=meta.get("timeframe", ""),
            settings=settings,
            trades=trades,
            stats=stats,
            raw_metadata=raw_metadata,
            source_path=str(path.resolve()),
        )

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _find_row_with_cell(rows: list[list[Any]], target: str) -> int | None:
        for idx, row in enumerate(rows):
            for cell in row:
                if isinstance(cell, str) and cell.strip() == target:
                    return idx
        return None

    @staticmethod
    def _extract_table(df_raw: pd.DataFrame, header_idx: int, end_idx: int | None = None) -> pd.DataFrame:
        """Slice a clean DataFrame out of the raw sheet given the header row index."""
        header_row = df_raw.iloc[header_idx + 1]
        # Drop completely-NaN columns to avoid phantom unnamed columns
        header_cols = [c if pd.notna(c) else f"__col_{i}" for i, c in enumerate(header_row)]
        if end_idx is not None:
            data = df_raw.iloc[header_idx + 2 : end_idx].copy()
        else:
            data = df_raw.iloc[header_idx + 2 :].copy()
        data.columns = header_cols
        data = data.loc[:, ~data.columns.str.startswith("__col_")]
        # Drop rows that are completely empty
        data = data.dropna(how="all").reset_index(drop=True)
        return data

    # ── meta / settings parsing ───────────────────────────────────────────

    @staticmethod
    def _parse_meta(rows: list[list[Any]], orders_header_idx: int) -> dict[str, Any]:
        """Extract symbol, period, strategy name, leverage, initial capital and settings."""
        meta: dict[str, Any] = {
            "strategy_name": "unknown",
            "symbol": "",
            "timeframe": "",
            "initial_capital": None,
            "leverage": None,
            "settings": {},
        }

        in_settings = False
        current_section: str | None = None

        for row in rows[:orders_header_idx]:
            label = row[0] if len(row) > 0 and pd.notna(row[0]) else None
            label_str = str(label).strip() if label is not None else ""

            # Section detectors
            if label_str == "Einstellungen":
                in_settings = True
                continue
            if label_str in ("Ergebnisse", "Firma:"):
                in_settings = False
                continue

            # Meta fields
            if label_str == "Expertenprogramm:":
                val = row[3] if len(row) > 3 and pd.notna(row[3]) else None
                meta["strategy_name"] = str(val) if val is not None else "unknown"
                continue
            if label_str == "Symbol:":
                val = row[3] if len(row) > 3 and pd.notna(row[3]) else None
                meta["symbol"] = str(val) if val is not None else ""
                continue
            if label_str == "Periode:":
                val = row[3] if len(row) > 3 and pd.notna(row[3]) else None
                if val is not None:
                    tf_match = re.search(r"^(\w+)\s*\(", str(val))
                    meta["timeframe"] = tf_match.group(1).lower() if tf_match else str(val)
                continue
            if label_str == "Ersteinlage:":
                val = row[3] if len(row) > 3 and pd.notna(row[3]) else None
                meta["initial_capital"] = float(val) if val is not None else None
                continue
            if label_str == "Hebel:":
                val = row[3] if len(row) > 3 and pd.notna(row[3]) else None
                meta["leverage"] = str(val) if val is not None else None
                continue

            # Settings / parameters (look in first 8 columns for key=value patterns)
            if in_settings:
                for cell in row[:12]:
                    if cell is None or (isinstance(cell, float) and pd.isna(cell)):
                        continue
                    val_str = str(cell).strip()
                    if "=" in val_str:
                        key, _, v = val_str.partition("=")
                        key = key.strip()
                        v = v.strip()
                        if not key:
                            continue
                        if not v:
                            # Section header like "Breakout Parameters="
                            current_section = key
                        else:
                            meta["settings"][key] = MT5XLSXImporter._coerce_setting(v)
                    elif val_str and current_section and val_str not in meta["settings"]:
                        # Stand-alone setting name
                        meta["settings"][val_str] = True

        return meta

    @staticmethod
    def _coerce_setting(value: str) -> Any:
        """Coerce a string setting value to bool, int, float, or str."""
        v_lower = value.lower()
        if v_lower in ("true", "yes", "on"):
            return True
        if v_lower in ("false", "no", "off"):
            return False
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return float(value)
        except ValueError:
            pass
        return value

    # ── stats parsing ─────────────────────────────────────────────────────

    @staticmethod
    def _parse_stats(rows: list[list[Any]], orders_header_idx: int) -> dict[str, Any]:
        stats: dict[str, Any] = {}
        for row in rows[:orders_header_idx]:
            # Stats can be in columns 0-3, 4-7 or 8-11
            label_value_groups = []
            for col_offset in (0, 4, 8):
                if len(row) <= col_offset:
                    continue
                label = row[col_offset]
                if label is None or (isinstance(label, float) and pd.isna(label)):
                    continue
                label_str = str(label).strip()
                val_col = col_offset + 3
                val = row[val_col] if len(row) > val_col and pd.notna(row[val_col]) else None
                label_value_groups.append((label_str, val))

            for label_str, val in label_value_groups:
                key = _STAT_LABEL_MAP.get(label_str.rstrip(":"))
                if key is None:
                    key = _STAT_LABEL_MAP.get(label_str.rstrip(" :"))
                if key is None:
                    continue

                if key == "winners_with_pct":
                    w, pct = MT5XLSXImporter._parse_count_pct(val)
                    if w is not None:
                        stats["winners"] = w
                    if pct is not None:
                        stats["win_rate"] = pct
                    continue
                if key == "losers_with_pct":
                    l, pct = MT5XLSXImporter._parse_count_pct(val)
                    if l is not None:
                        stats["losers"] = l
                    continue
                if key == "max_drawdown_pct":
                    pct = MT5XLSXImporter._extract_first_percent(val)
                    if pct is not None:
                        stats["max_drawdown_pct"] = pct
                    continue
                if key == "max_drawdown_max":
                    pct = MT5XLSXImporter._extract_first_percent(val)
                    if pct is not None:
                        stats["max_drawdown_max_pct"] = pct
                    abs_val = MT5XLSXImporter._extract_parenthesized_number(val)
                    if abs_val is not None:
                        stats["max_drawdown_abs"] = abs_val
                    continue

                parsed = MT5XLSXImporter._coerce_stat(val)
                if parsed is not None:
                    stats[key] = parsed

        # Derive total_return if possible
        if "total_pnl" in stats and "initial_capital" in stats:
            ic = stats.get("initial_capital")
            if isinstance(ic, (int, float)) and ic != 0:
                stats["total_return"] = (stats["total_pnl"] / ic) * 100.0

        return stats

    @staticmethod
    def _parse_count_pct(val: Any) -> tuple[int | None, float | None]:
        """Parse strings like '222 (45.12%)' into (222, 45.12)."""
        if val is None:
            return None, None
        text = str(val)
        m = re.match(r"(\d+)\s*\(([^)]+)\)", text)
        if m:
            count = int(m.group(1))
            pct_str = m.group(2).replace("%", "").strip()
            try:
                pct = float(pct_str)
            except ValueError:
                pct = None
            return count, pct
        # Fallback: just an int
        try:
            return int(text), None
        except ValueError:
            return None, None

    @staticmethod
    def _extract_first_percent(val: Any) -> float | None:
        if val is None:
            return None
        m = re.search(r"([0-9]+[.,]?[0-9]*)\s*%", str(val))
        if m:
            try:
                return float(m.group(1).replace(",", "."))
            except ValueError:
                return None
        return None

    @staticmethod
    def _extract_parenthesized_number(val: Any) -> float | None:
        if val is None:
            return None
        m = re.search(r"\(([^)]+)\)", str(val))
        if m:
            raw = m.group(1).replace(" ", "").replace(",", ".")
            try:
                return float(raw)
            except ValueError:
                return None
        return None

    @staticmethod
    def _coerce_stat(val: Any) -> Any:
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return val
        text = str(val).strip()
        # Percentage wrapped in text, e.g. "1.0004 (0.04%)"
        m = re.match(r"([0-9]+[.,]?[0-9]*)\s*\(([^)]+)\)", text)
        if m:
            try:
                return float(m.group(1).replace(",", "."))
            except ValueError:
                return text
        # Plain number
        try:
            return float(text.replace(",", ".").replace(" ", ""))
        except ValueError:
            return text if text else None

    # ── SL/TP from Orders ─────────────────────────────────────────────────

    @staticmethod
    def _build_sltp_map(orders_df: pd.DataFrame) -> dict[Any, dict[str, float | None]]:
        mapping: dict[Any, dict[str, float | None]] = {}
        if orders_df.empty:
            return mapping

        # Identify column names heuristically
        col_order_id = MT5XLSXImporter._find_col(orders_df, ["Auftrag"])
        col_sl = MT5XLSXImporter._find_col(orders_df, ["S / L", "S/L"])
        col_tp = MT5XLSXImporter._find_col(orders_df, ["T / P", "T/P"])

        if col_order_id is None:
            return mapping

        for _, row in orders_df.iterrows():
            oid = row[col_order_id]
            if pd.isna(oid):
                continue
            sl = row.get(col_sl, None) if col_sl else None
            tp = row.get(col_tp, None) if col_tp else None
            mapping[oid] = {
                "sl": float(sl) if pd.notna(sl) else None,
                "tp": float(tp) if pd.notna(tp) else None,
            }
        return mapping

    # ── trade reconstruction ──────────────────────────────────────────────

    @staticmethod
    def _build_trades(
        trades_df: pd.DataFrame,
        sltp_map: dict[Any, dict[str, float | None]],
        source_timezone: str | None = None,
    ) -> list[Trade]:
        if trades_df.empty:
            return []

        col_time = MT5XLSXImporter._find_col(trades_df, ["Zeit"])
        col_trade_id = MT5XLSXImporter._find_col(trades_df, ["Trade"])
        col_symbol = MT5XLSXImporter._find_col(trades_df, ["Symbol"])
        col_typ = MT5XLSXImporter._find_col(trades_df, ["Typ"])
        col_direction = MT5XLSXImporter._find_col(trades_df, ["Richtung"])
        col_volume = MT5XLSXImporter._find_col(trades_df, ["Volumen"])
        col_price = MT5XLSXImporter._find_col(trades_df, ["Preis"])
        col_order_id = MT5XLSXImporter._find_col(trades_df, ["Auftrag"])
        col_commission = MT5XLSXImporter._find_col(trades_df, ["Kommission"])
        col_swap = MT5XLSXImporter._find_col(trades_df, ["Swap"])
        col_profit = MT5XLSXImporter._find_col(trades_df, ["Gewinn"])
        col_comment = MT5XLSXImporter._find_col(trades_df, ["Kommentar"])

        if col_time is None or col_direction is None:
            raise ValueError("Trades table is missing required columns (Zeit / Richtung).")

        trades: list[Trade] = []
        i = 0
        trade_id_counter = 1

        rows_list = trades_df.to_dict(orient="records")
        n = len(rows_list)

        while i < n:
            row = rows_list[i]
            typ = MT5XLSXImporter._get_str(row, col_typ, "").lower()
            direction = MT5XLSXImporter._get_str(row, col_direction, "").lower()

            # Skip balance / non-trade rows
            if typ == "balance" or direction not in ("in", "out"):
                i += 1
                continue

            # We expect an "in" row here
            if direction != "in":
                i += 1
                continue

            entry_row = row
            j = i + 1
            exit_row = None
            while j < n:
                nxt = rows_list[j]
                nxt_typ = MT5XLSXImporter._get_str(nxt, col_typ, "").lower()
                nxt_dir = MT5XLSXImporter._get_str(nxt, col_direction, "").lower()
                if nxt_typ == "balance":
                    j += 1
                    continue
                if nxt_dir == "out":
                    exit_row = nxt
                    break
                if nxt_dir == "in":
                    # Unexpected next entry without exit — skip current entry
                    break
                j += 1

            if exit_row is None:
                i = j if j > i else i + 1
                continue

            # Build Trade
            symbol = MT5XLSXImporter._get_str(entry_row, col_symbol, "")
            entry_time_raw = MT5XLSXImporter._get_str(entry_row, col_time, "")
            exit_time_raw = MT5XLSXImporter._get_str(exit_row, col_time, "")

            entry_price = MT5XLSXImporter._to_float(entry_row.get(col_price))
            exit_price = MT5XLSXImporter._to_float(exit_row.get(col_price))
            volume = MT5XLSXImporter._to_float(entry_row.get(col_volume))
            pnl = MT5XLSXImporter._to_float(exit_row.get(col_profit))

            typ_entry = MT5XLSXImporter._get_str(entry_row, col_typ, "").lower()
            direction_str = "LONG" if typ_entry == "buy" else "SHORT"

            commission = MT5XLSXImporter._to_float(exit_row.get(col_commission), default=0.0)
            swap = MT5XLSXImporter._to_float(exit_row.get(col_swap), default=0.0)
            fees = commission + swap

            # Exit reason heuristics
            exit_comment = MT5XLSXImporter._get_str(exit_row, col_comment, "")
            exit_reason = MT5XLSXImporter._infer_exit_reason(exit_comment)

            entry_comment = MT5XLSXImporter._get_str(entry_row, col_comment, "")
            tags = [entry_comment] if entry_comment else []

            # SL/TP lookup via order id
            order_id = entry_row.get(col_order_id) if col_order_id else None
            sl = tp = None
            if order_id is not None and pd.notna(order_id):
                mapped = sltp_map.get(order_id, {})
                sl = mapped.get("sl")
                tp = mapped.get("tp")

            exit_obj = TradeExit(
                time=MT5XLSXImporter._to_iso(exit_time_raw, source_timezone),
                price=exit_price,
                quantity=volume if volume is not None else 0.0,
                pnl=pnl,
                fees=fees if fees != 0.0 else None,
                reason=exit_reason,
            )

            trade = Trade(
                id=trade_id_counter,
                symbol=symbol,
                direction=direction_str,
                entry_time=MT5XLSXImporter._to_iso(entry_time_raw, source_timezone),
                entry_price=entry_price if entry_price is not None else 0.0,
                quantity=volume if volume is not None else 0.0,
                exits=[exit_obj],
                pnl=pnl if pnl is not None else 0.0,
                fees=fees if fees != 0.0 else None,
                net_pnl=(pnl - fees) if (pnl is not None and fees is not None) else None,
                exit_reason=exit_reason,
                tags=tags,
                meta={
                    "order_id": order_id,
                    "stop_loss": sl,
                    "take_profit": tp,
                    "swap": swap if swap != 0.0 else None,
                    "commission": commission if commission != 0.0 else None,
                },
            )
            trades.append(trade)
            trade_id_counter += 1
            i = j + 1

        return trades

    @staticmethod
    def _get_str(row: dict[str, Any], col: str | None, default: str) -> str:
        if col is None:
            return default
        val = row.get(col)
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return default
        return str(val).strip()

    @staticmethod
    def _to_float(val: Any, default: float | None = None) -> float | None:
        if val is None or (isinstance(val, float) and pd.isna(val)):
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _to_iso(raw: str, source_tz: str | None = None) -> str:
        """Convert MT5 datetime string to ISO-8601 UTC string."""
        text = raw.strip() if raw else ""
        for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                if source_tz:
                    import zoneinfo
                    tz = zoneinfo.ZoneInfo(source_tz)
                    dt = dt.replace(tzinfo=tz)
                    dt = dt.astimezone(timezone.utc)
                else:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.isoformat().replace("+00:00", "Z")
            except ValueError:
                continue
        # If parsing fails, return as-is (tests will catch obvious issues)
        return text

    @staticmethod
    def _infer_exit_reason(comment: str) -> str | None:
        comment = comment.lower()
        if comment.startswith("sl "):
            return "stop_loss"
        if comment.startswith("tp "):
            return "take_profit"
        if comment.startswith("trailing "):
            return "trailing_stop"
        return None

    @staticmethod
    def _find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
        cols = list(df.columns)
        for cand in candidates:
            for c in cols:
                if c == cand or str(c).strip() == cand:
                    return c
        return None
