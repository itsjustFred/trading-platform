"""MT5 Strategy Tester XLSX importer."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from shared.models import Trade, TradeExit
from validation.importers._numbers import detect_decimal_comma, parse_locale_number
from validation.importers._pairing import closed_positions, group_position_fills
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


def _resolve_source_timezone(source_tz: str | tzinfo | None) -> tzinfo | None:
    if source_tz is None:
        return None
    if isinstance(source_tz, tzinfo):
        return source_tz
    try:
        return ZoneInfo(str(source_tz))
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown source timezone: {source_tz}") from exc


class MT5XLSXImporter(BaseImporter):
    """Import MT5 Strategy Tester XLSX reports (German UI)."""

    def load(self, source: str | Path, source_timezone: str | tzinfo | None = None, **kwargs: Any) -> ValidationReference:
        """Read an MT5 XLSX report and return a ``ValidationReference``.

        Parameters
        ----------
        source:
            Path to the ``.xlsx`` file.
        source_timezone:
            Optional IANA timezone name (e.g. "Europe/Athens") or ``tzinfo`` of the report's timestamps.
            If provided, naive timestamps will be interpreted in this timezone and
            converted to UTC. Defaults to UTC if not provided.
        **kwargs:
            Forwarded to ``pandas.read_excel`` (e.g. ``engine``).
        """
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"MT5 XLSX report not found: {path}")

        resolved_timezone = _resolve_source_timezone(source_timezone)
        df_raw = pd.read_excel(path, header=None, sheet_name=0, **kwargs)
        rows = df_raw.values.tolist()

        # Locate section headers dynamically
        orders_header_idx = self._find_row_with_cell(rows, _ORDERS_LABEL)
        trades_header_idx = self._find_row_with_cell(rows, _TRADES_LABEL)

        if orders_header_idx is None:
            raise ValueError("Could not locate 'Orders' section in the report.")
        if trades_header_idx is None:
            raise ValueError("Could not locate 'Trades' section in the report.")
        # The Trades section must come after the Orders section: every
        # downstream slice (`_extract_table`, `_parse_meta`, the deal walker)
        # assumes report order.
        if trades_header_idx < orders_header_idx:
            raise ValueError(
                "Trades section found before Orders section; "
                "the report layout is not recognisable."
            )

        # Parse meta, settings, stats from header area
        meta = self._parse_meta(rows, orders_header_idx)
        settings = meta.pop("settings", {})
        stats = self._parse_stats(rows, orders_header_idx)
        raw_metadata = {
            "parsed_meta": meta,
            "parsed_stats": stats,
            "source_timezone": str(resolved_timezone or timezone.utc),
        }

        # Parse Orders table for SL/TP from Orders table
        orders_df = self._extract_table(df_raw, orders_header_idx, end_idx=trades_header_idx)
        sltp_map = self._build_sltp_map(orders_df)

        # Parse Trades / Deals table
        trades_df = self._extract_table(df_raw, trades_header_idx, end_idx=None)
        trades = self._build_trades(trades_df, sltp_map, resolved_timezone)

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

            if MT5XLSXImporter._apply_meta_row(row, label_str, meta):
                continue

            if in_settings:
                current_section = MT5XLSXImporter._parse_settings_row(
                    row, current_section, meta["settings"]
                )

        return meta

    @staticmethod
    def _meta_val(row: list[Any]) -> Any:
        return row[3] if len(row) > 3 and pd.notna(row[3]) else None

    @staticmethod
    def _apply_meta_row(row: list[Any], label_str: str, meta: dict[str, Any]) -> bool:
        """Apply one meta row (label in column 0); True when the row was a meta label."""
        val = MT5XLSXImporter._meta_val(row)
        if label_str == "Expertenprogramm:":
            meta["strategy_name"] = str(val) if val is not None else "unknown"
        elif label_str == "Symbol:":
            meta["symbol"] = str(val) if val is not None else ""
        elif label_str == "Periode:":
            if val is not None:
                tf_match = re.search(r"^(\w+)\s*\(", str(val))
                meta["timeframe"] = tf_match.group(1).lower() if tf_match else str(val)
        elif label_str == "Ersteinlage:":
            meta["initial_capital"] = float(val) if val is not None else None
        elif label_str == "Hebel:":
            meta["leverage"] = str(val) if val is not None else None
        else:
            return False
        return True

    @staticmethod
    def _parse_settings_row(
        row: list[Any], current_section: str | None, settings: dict[str, Any]
    ) -> str | None:
        """Parse one settings row; returns the (possibly new) current section."""
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
                    settings[key] = MT5XLSXImporter._coerce_setting(v)
            elif val_str and current_section and val_str not in settings:
                # Stand-alone setting name
                settings[val_str] = True
        return current_section

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
            for label_str, val in MT5XLSXImporter._stat_label_value_groups(row):
                key = _STAT_LABEL_MAP.get(label_str.rstrip(":"))
                if key is None:
                    key = _STAT_LABEL_MAP.get(label_str.rstrip(" :"))
                if key is None:
                    continue

                MT5XLSXImporter._apply_stat_key(key, val, stats)

        # Derive total_return if possible
        if "total_pnl" in stats and "initial_capital" in stats:
            ic = stats.get("initial_capital")
            if isinstance(ic, (int, float)) and ic != 0:
                stats["total_return"] = (stats["total_pnl"] / ic) * 100.0

        return stats

    @staticmethod
    def _stat_label_value_groups(row: list[Any]) -> list[tuple[str, Any]]:
        """Extract (label, value) pairs; stats can be in columns 0-3, 4-7 or 8-11."""
        label_value_groups: list[tuple[str, Any]] = []
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
        return label_value_groups

    @staticmethod
    def _apply_stat_key(key: str, val: Any, stats: dict[str, Any]) -> None:
        """Apply one mapped stat key/value to the stats dict."""
        if key == "winners_with_pct":
            w, pct = MT5XLSXImporter._parse_count_pct(val)
            if w is not None:
                stats["winners"] = w
            if pct is not None:
                stats["win_rate"] = pct
            return
        if key == "losers_with_pct":
            l, _pct = MT5XLSXImporter._parse_count_pct(val)
            if l is not None:
                stats["losers"] = l
            return
        if key == "max_drawdown_pct":
            pct = MT5XLSXImporter._extract_first_percent(val)
            if pct is not None:
                stats["max_drawdown_pct"] = pct
            return
        if key == "max_drawdown_max":
            MT5XLSXImporter._apply_max_drawdown_max(val, stats)
            return

        parsed = MT5XLSXImporter._coerce_stat(val)
        if parsed is not None:
            stats[key] = parsed

    @staticmethod
    def _apply_max_drawdown_max(val: Any, stats: dict[str, Any]) -> None:
        pct = MT5XLSXImporter._extract_first_percent(val)
        if pct is not None:
            stats["max_drawdown_max_pct"] = pct
        abs_val = MT5XLSXImporter._extract_parenthesized_number(val)
        if abs_val is not None:
            stats["max_drawdown_abs"] = abs_val

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

        # DV-B44: the xlsx SL/TP cells go through the same locale-aware
        # parser as the HTML importer; "2.500" is 2500, not 2.5.
        #
        # Only the SL/TP cells feed the locale detector: a grouped value in
        # another orders column (volume "0.10", comment "10.000 runs") has no
        # bearing on how the SL/TP columns are formatted, and a stray ASCII
        # grouping there would flip the convention for the whole report.
        cell_values: list[str | None] = []
        for _, row in orders_df.iterrows():
            for col in (col_sl, col_tp):
                if col is not None and col in row and pd.notna(row[col]):
                    cell_values.append(str(row[col]))
        decimal_comma = detect_decimal_comma(cell_values)

        def _parse(cell: Any, order_id: Any) -> float | None:
            if cell is None or (isinstance(cell, float) and pd.isna(cell)):
                return None
            parsed = parse_locale_number(str(cell), decimal_comma)
            if parsed is None:
                # A non-empty but unparseable cell keeps the order's SL/TP
                # unset; log it so the skip stays observable.
                logger.warning("Order %s: unparseable SL/TP value %r", order_id, cell)
            return parsed

        for _, row in orders_df.iterrows():
            oid = row[col_order_id]
            if pd.isna(oid):
                continue
            sl = row.get(col_sl, None) if col_sl else None
            tp = row.get(col_tp, None) if col_tp else None
            mapping[oid] = {
                "sl": _parse(sl, oid),
                "tp": _parse(tp, oid),
            }
        return mapping

    # ── trade reconstruction ──────────────────────────────────────────────

    @staticmethod
    def _build_trades(
        trades_df: pd.DataFrame,
        sltp_map: dict[Any, dict[str, float | None]],
        source_timezone: str | tzinfo | None = None,
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

        # DV-B42: direction is derived from the Typ column, so it must be
        # present.  Without this check a missing or locale-variant Typ column
        # silently relabels every trade as SHORT.
        if col_time is None or col_direction is None or col_typ is None:
            raise ValueError(
                "Trades table is missing required columns (Zeit / Typ / Richtung)."
            )

        # DV-B43: reconstruct one Trade per position with one TradeExit per
        # out row; the positional walker previously dropped every fill after
        # a second consecutive "in".
        #
        # The Trade column holds *deal* ids (autoincrement, unique per row —
        # see docs/mt5_xlsx_schema.md), so it cannot group the fills of a
        # position.  Positions are instead read off the deal sequence: every
        # "in" opens a new position and the following "out" rows close the
        # most recently opened one (MT5 reconstructs a position this way;
        # partial entries/exits keep their order in the report).
        #
        # Some exporters repeat the position id instead of the deal id; those
        # reports are grouped by the shared id first and only fall back to
        # the sequential walker when the ids are unique per row.
        rows_list = trades_df.to_dict(orient="records")

        def _row_direction(row: dict[str, Any]) -> str:
            return MT5XLSXImporter._get_str(row, col_direction, "").lower()

        def _deal_id(row: dict[str, Any]) -> str:
            value = row.get(col_trade_id)
            if value is None or (isinstance(value, float) and pd.isna(value)):
                return ""
            return str(value).strip()

        # Prefer shared position ids when the report has them (every deal of
        # a position carries the same id); fall back to the sequential deal
        # walker when the ids are unique per row (a pure deal counter).
        def _is_trade_row(row: dict[str, Any]) -> bool:
            return MT5XLSXImporter._get_str(row, col_typ, "").lower() != "balance"

        id_counts: dict[str, int] = {}
        for row in rows_list:
            if not _is_trade_row(row):
                continue
            deal_id = _deal_id(row)
            if deal_id:
                id_counts[deal_id] = id_counts.get(deal_id, 0) + 1
        shared_ids = any(count > 1 for count in id_counts.values())

        if shared_ids:
            position_id_of: Callable[[dict[str, Any]], str] = _deal_id
        else:
            # Sequential deal walker: an "out" closes the *oldest* position
            # whose entries are not yet fully exited, so an in/in/out/out
            # sequence reconstructs one position entered with two fills and
            # closed with two exits, and the next "in" starts the next
            # position.
            entry_volume: list[float] = []  # summed entry volume per position
            exit_volume: list[float] = []  # summed exit volume per position

            def _assign_sequential_position(row: dict[str, Any]) -> str:
                """Assign a position id from the deal sequence.

                An "in" deal opens a new position once every existing one is
                fully exited; an "out" deal closes volume on the oldest open
                position.  An orphan "out" with no open position gets no
                position id, so its fill is skipped instead of inflating the
                pnl of the next reconstructed position.
                """
                if _deal_id(row) == "":
                    return ""
                direction = _row_direction(row)
                volume = MT5XLSXImporter._to_float(row.get(col_volume)) or 0.0
                if direction == "in":
                    for index, remaining in enumerate(entry_volume):
                        if exit_volume[index] < remaining:
                            break
                    else:
                        index = len(entry_volume)
                        entry_volume.append(0.0)
                        exit_volume.append(0.0)
                    entry_volume[index] += volume
                    return f"position-{index + 1}"
                for index, remaining in enumerate(entry_volume):
                    if exit_volume[index] < remaining:
                        exit_volume[index] += volume
                        return f"position-{index + 1}"
                logger.warning(
                    "Orphan 'out' deal %r (%s %s at %s): no open position to "
                    "close; the fill is skipped.",
                    _deal_id(row),
                    volume,
                    MT5XLSXImporter._get_str(row, col_symbol, ""),
                    MT5XLSXImporter._get_str(row, col_time, ""),
                )
                return ""

            position_id_of = _assign_sequential_position

        position_fills = group_position_fills(
            rows_list,
            position_id_of=position_id_of,
            direction_of=_row_direction,
            is_trade_row=_is_trade_row,
        )

        trades: list[Trade] = []
        trade_id_counter = 1

        for fills in closed_positions(position_fills):
            entry_row = fills.entries[0]
            exit_rows = fills.exits

            symbol = MT5XLSXImporter._get_str(entry_row, col_symbol, "")
            entry_time_raw = MT5XLSXImporter._get_str(entry_row, col_time, "")

            entry_price = MT5XLSXImporter._to_float(entry_row.get(col_price))
            entry_volume = sum(
                (MT5XLSXImporter._to_float(r.get(col_volume)) or 0.0) for r in fills.entries
            )

            typ_entry = MT5XLSXImporter._get_str(entry_row, col_typ, "").lower()
            # DV-B42: only plain buy/sell entries carry a derivable direction.
            # Anything else (pending-order types, locale variants) must fail
            # the import instead of silently becoming SHORT.
            if typ_entry == "buy":
                direction_str = "LONG"
            elif typ_entry == "sell":
                direction_str = "SHORT"
            else:
                raise ValueError(
                    f"Unsupported entry Typ {typ_entry!r} for trade "
                    f"{trade_id_counter} (expected 'buy' or 'sell'); cannot "
                    "derive trade direction."
                )

            # DV-B43: commission/swap accumulate over every fill of the
            # position (entry fills included), not just the last exit row.
            commission = sum(
                (MT5XLSXImporter._to_float(r.get(col_commission), default=0.0) or 0.0)
                for r in fills.entries + exit_rows
            )
            swap = sum(
                (MT5XLSXImporter._to_float(r.get(col_swap), default=0.0) or 0.0)
                for r in fills.entries + exit_rows
            )
            fees = commission + swap

            exits: list[TradeExit] = []
            for exit_row in exit_rows:
                exit_time_raw = MT5XLSXImporter._get_str(exit_row, col_time, "")
                exit_price = MT5XLSXImporter._to_float(exit_row.get(col_price))
                exit_volume = MT5XLSXImporter._to_float(exit_row.get(col_volume))
                exit_pnl = MT5XLSXImporter._to_float(exit_row.get(col_profit))

                exit_comment = MT5XLSXImporter._get_str(exit_row, col_comment, "")
                exit_reason = MT5XLSXImporter._infer_exit_reason(exit_comment)
                exits.append(
                    TradeExit(
                        time=MT5XLSXImporter._to_iso(exit_time_raw, source_timezone),
                        price=exit_price if exit_price is not None else 0.0,
                        quantity=exit_volume if exit_volume is not None else 0.0,
                        pnl=exit_pnl,
                        reason=exit_reason,
                    )
                )
            last_exit_comment = MT5XLSXImporter._get_str(exit_rows[-1], col_comment, "")
            exit_reason = MT5XLSXImporter._infer_exit_reason(last_exit_comment)

            pnl = sum((e.pnl or 0.0) for e in exits)

            entry_comment = MT5XLSXImporter._get_str(entry_row, col_comment, "")
            tags = [entry_comment] if entry_comment else []

            # SL/TP lookup via the entry fill's order id
            order_id = entry_row.get(col_order_id) if col_order_id else None
            sl = tp = None
            if order_id is not None and pd.notna(order_id):
                mapped = sltp_map.get(order_id, {})
                sl = mapped.get("sl")
                tp = mapped.get("tp")

            trade = Trade(
                id=trade_id_counter,
                symbol=symbol,
                direction=direction_str,
                entry_time=MT5XLSXImporter._to_iso(entry_time_raw, source_timezone),
                entry_price=entry_price if entry_price is not None else 0.0,
                quantity=entry_volume,
                exits=exits,
                pnl=pnl,
                fees=fees if fees != 0.0 else None,
                net_pnl=pnl - fees,
                exit_reason=exit_reason,
                tags=tags,
                meta={
                    "position_id": fills.position_id,
                    "order_id": order_id,
                    "stop_loss": sl,
                    "take_profit": tp,
                    "swap": swap if swap != 0.0 else None,
                    "commission": commission if commission != 0.0 else None,
                },
            )
            trades.append(trade)
            trade_id_counter += 1

        return trades

    @staticmethod
    def _find_exit_row(
        rows_list: list[dict[str, Any]],
        start: int,
        col_typ: str | None,
        col_direction: str | None,
    ) -> tuple[dict[str, Any] | None, int]:
        """Find the exit row matching the entry at ``start - 1``.

        Returns ``(exit_row, next_index)`` where ``next_index`` is the index the
        caller should continue scanning from when no exit row was found.
        """
        j = start
        n = len(rows_list)
        while j < n:
            nxt = rows_list[j]
            nxt_typ = MT5XLSXImporter._get_str(nxt, col_typ, "").lower()
            nxt_dir = MT5XLSXImporter._get_str(nxt, col_direction, "").lower()
            if nxt_typ == "balance":
                j += 1
                continue
            if nxt_dir == "out":
                return nxt, j
            if nxt_dir == "in":
                # Unexpected next entry without exit — skip current entry
                return None, j
            j += 1
        return None, j

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
            # DV-B44: German-locale cells ("1.234,56", "2.500") are not
            # float()-parsable; fall back to the locale-aware parser so the
            # value is neither dropped nor silently corrupted.
            if isinstance(val, str):
                return parse_locale_number(val, decimal_comma=True)
            return default

    @staticmethod
    def _to_iso(raw: str, source_tz: str | tzinfo | None = None) -> str:
        """Convert MT5 datetime string to ISO-8601 UTC string."""
        text = raw.strip() if raw else ""
        tz = _resolve_source_timezone(source_tz)
        for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                if tz:
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
