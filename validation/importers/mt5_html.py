"""MT5 Strategy Tester HTML importer (German UI)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from shared.models import Trade, TradeExit
from validation.importers._numbers import detect_decimal_comma, parse_locale_number
from validation.importers._pairing import closed_positions, group_position_fills
from validation.importers.base import BaseImporter
from validation.reference import ValidationReference

logger = logging.getLogger(__name__)

# German labels we scan for when locating tables
_ORDERS_LABELS = ("Orders", "Order", "Aufträge")
_TRADES_LABELS = ("Trades", "Deals", "Geschäfte")

# Known German stat labels → canonical keys
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

_STAT_LABEL_MAP: dict[str, str] = {}
for label, key in _STAT_LABELS:
    _STAT_LABEL_MAP[label.rstrip(":")] = key
    _STAT_LABEL_MAP[label] = key
    _STAT_LABEL_MAP[label.rstrip(" :")] = key


class MT5HTMLImporter(BaseImporter):
    """Import MT5 Strategy Tester HTML reports (German UI)."""

    def load(self, source: str | Path, source_timezone: str | None = None, **kwargs: Any) -> ValidationReference:
            """Read an MT5 HTML report and return a ``ValidationReference``.

            Parameters
            ----------
            source:
                Path to the ``.html`` file.
            source_timezone:
                Optional IANA timezone name (e.g. \"Europe/Athens\") of the report's timestamps.
                If provided, naive timestamps will be interpreted in this timezone and
                converted to UTC. Defaults to UTC if not provided.
            **kwargs:
                Forwarded to ``BeautifulSoup`` parser selection.
            """
            path = Path(source)
            if not path.exists():
                raise FileNotFoundError(f"MT5 HTML report not found: {path}")

            html_text = path.read_text(encoding="utf-8", errors="replace")
            soup = BeautifulSoup(html_text, kwargs.pop("features", "lxml"))

            meta = self._parse_meta(soup)
            settings = meta.pop("settings", {})
            stats = self._parse_stats(soup)
            raw_metadata = {"parsed_meta": meta, "parsed_stats": stats}

            # Locate Orders and Trades tables
            orders_table = self._find_table_by_header_keywords(soup, _ORDERS_LABELS, required_cols=["Auftrag"])
            trades_table = self._find_table_by_header_keywords(soup, _TRADES_LABELS, required_cols=["Zeit", "Richtung"])

            if orders_table is None:
                raise ValueError("Could not locate 'Orders' table in the HTML report.")
            if trades_table is None:
                raise ValueError("Could not locate 'Trades' table in the HTML report.")

            sltp_map = self._build_sltp_map(orders_table)
            trades = self._build_trades(trades_table, sltp_map, source_timezone)

            return ValidationReference(
                strategy_name=meta.get("strategy_name", "unknown"),
                source_framework="mt5_html",
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
    def _find_table_by_header_keywords(
        soup: BeautifulSoup,
        keywords: tuple[str, ...],
        required_cols: list[str] | None = None,
    ) -> BeautifulSoup | None:
        """Find the first <table> whose header contains a keyword *and* required columns."""
        for table in soup.find_all("table"):
            thead = table.find("thead")
            header_row = None
            if thead:
                header_row = thead.find("tr")
            if header_row is None:
                rows = table.find_all("tr", recursive=False)
                if not rows:
                    rows = table.find_all("tr")
                if rows:
                    header_row = rows[0]
            if header_row is None:
                continue

            header_text = header_row.get_text(separator=" ", strip=True).lower()
            header_cells = [cell.get_text(strip=True).lower() for cell in header_row.find_all(("td", "th"))]

            # Also check preceding sibling heading or caption
            preceding_text = ""
            prev = table.find_previous_sibling()
            for _ in range(3):
                if prev is None:
                    break
                tag_name = prev.name or ""
                if tag_name in ("h1", "h2", "h3", "h4", "caption", "div", "p"):
                    preceding_text += " " + prev.get_text(separator=" ", strip=True).lower()
                prev = prev.find_previous_sibling()

            combined = header_text + " " + preceding_text
            keyword_match = any(kw.lower() in combined for kw in keywords)
            if not keyword_match:
                continue

            if required_cols:
                if not any(req.lower() in header_cells for req in required_cols):
                    continue

            return table
        return None

    @staticmethod
    def _table_to_rows(table: BeautifulSoup) -> list[list[str]]:
        """Convert a <table> to a list of string-tokenised rows."""
        rows: list[list[str]] = []
        for tr in table.find_all("tr"):
            cells = tr.find_all(("td", "th"))
            row = [cell.get_text(separator=" ", strip=True) for cell in cells]
            if any(cell for cell in row):
                rows.append(row)
        return rows

    # ── meta parsing ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_meta(soup: BeautifulSoup) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "strategy_name": "unknown",
            "symbol": "",
            "timeframe": "",
            "initial_capital": None,
            "leverage": None,
            "settings": {},
        }

        # Scan all tables for key/value rows like "Expertenprogramm: value"
        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                cells = tr.find_all(("td", "th"))
                if len(cells) < 2:
                    continue
                label = cells[0].get_text(strip=True)
                val = cells[1].get_text(strip=True) if len(cells) > 1 else ""

                if label == "Expertenprogramm:":
                    meta["strategy_name"] = val if val else "unknown"
                elif label == "Symbol:":
                    meta["symbol"] = val
                elif label == "Periode:":
                    tf_match = re.search(r"^(\w+)\s*\(", val)
                    meta["timeframe"] = tf_match.group(1).lower() if tf_match else val.lower()
                elif label == "Ersteinlage:":
                    try:
                        meta["initial_capital"] = float(val.replace(" ", "").replace(",", "."))
                    except (ValueError, TypeError):
                        meta["initial_capital"] = None
                elif label == "Hebel:":
                    meta["leverage"] = val

        # Scan for settings sections (key=value patterns in any table cell)
        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                for cell in tr.find_all(("td", "th")):
                    text = cell.get_text(strip=True)
                    if "=" in text and not text.startswith("http"):
                        key, _, v = text.partition("=")
                        key = key.strip()
                        v = v.strip()
                        if key and v:
                            meta["settings"][key] = MT5HTMLImporter._coerce_setting(v)

        return meta

    @staticmethod
    def _coerce_setting(value: str) -> Any:
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
            return float(value.replace(",", "."))
        except ValueError:
            pass
        return value

    # ── stats parsing ─────────────────────────────────────────────────────

    @staticmethod
    def _parse_stats(soup: BeautifulSoup) -> dict[str, Any]:
        stats: dict[str, Any] = {}

        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                cells = tr.find_all(("td", "th"))
                if not cells:
                    continue
                # Stats can be laid out as label-value pairs across columns
                for i in range(0, len(cells) - 1, 2):
                    label = cells[i].get_text(strip=True)
                    val = cells[i + 1].get_text(strip=True) if (i + 1) < len(cells) else ""
                    MT5HTMLImporter._apply_stat(label, val, stats)
                # Also handle 4-column layouts: label, val, label, val
                if len(cells) == 4:
                    MT5HTMLImporter._apply_stat(cells[0].get_text(strip=True), cells[1].get_text(strip=True), stats)
                    MT5HTMLImporter._apply_stat(cells[2].get_text(strip=True), cells[3].get_text(strip=True), stats)

        return stats

    @staticmethod
    def _apply_stat(label: str, val: str, stats: dict[str, Any]) -> None:
        if not label or not val:
            return
        key = _STAT_LABEL_MAP.get(label.rstrip(":"))
        if key is None:
            key = _STAT_LABEL_MAP.get(label.rstrip(" :"))
        if key is None:
            return

        if key == "winners_with_pct":
            w, pct = MT5HTMLImporter._parse_count_pct(val)
            if w is not None:
                stats["winners"] = w
            if pct is not None:
                stats["win_rate"] = pct
            return
        if key == "losers_with_pct":
            l, pct = MT5HTMLImporter._parse_count_pct(val)
            if l is not None:
                stats["losers"] = l
            return
        if key in ("max_drawdown_pct", "max_drawdown_max"):
            pct = MT5HTMLImporter._extract_first_percent(val)
            if pct is not None:
                if key == "max_drawdown_pct":
                    stats["max_drawdown_pct"] = pct
                else:
                    stats["max_drawdown_max_pct"] = pct
            abs_val = MT5HTMLImporter._extract_parenthesized_number(val)
            if abs_val is not None:
                stats["max_drawdown_abs"] = abs_val
            return

        parsed = MT5HTMLImporter._coerce_stat(val)
        if parsed is not None:
            stats[key] = parsed

    @staticmethod
    def _parse_count_pct(val: str) -> tuple[int | None, float | None]:
        if not val:
            return None, None
        m = re.match(r"(\d+)\s*\(([^)]+)\)", val)
        if m:
            count = int(m.group(1))
            pct_str = m.group(2).replace("%", "").strip()
            try:
                pct = float(pct_str.replace(",", "."))
            except ValueError:
                pct = None
            return count, pct
        try:
            return int(val), None
        except ValueError:
            return None, None

    @staticmethod
    def _extract_first_percent(val: str) -> float | None:
        if not val:
            return None
        m = re.search(r"([0-9]+[.,]?[0-9]*)\s*%", val)
        if m:
            try:
                return float(m.group(1).replace(",", "."))
            except ValueError:
                return None
        return None

    @staticmethod
    def _extract_parenthesized_number(val: str) -> float | None:
        if not val:
            return None
        m = re.search(r"\(([^)]+)\)", val)
        if m:
            raw = m.group(1).replace(" ", "").replace(",", ".")
            try:
                return float(raw)
            except ValueError:
                return None
        return None

    @staticmethod
    def _coerce_stat(val: str) -> Any:
        if not val:
            return None
        text = val.strip()
        m = re.match(r"([0-9]+[.,]?[0-9]*)\s*\(([^)]+)\)", text)
        if m:
            try:
                return float(m.group(1).replace(",", "."))
            except ValueError:
                return text
        try:
            return float(text.replace(",", ".").replace(" ", ""))
        except ValueError:
            return text if text else None

    # ── SL/TP from Orders ─────────────────────────────────────────────────

    @staticmethod
    def _build_sltp_map(orders_table: BeautifulSoup) -> dict[str, dict[str, float | None]]:
        mapping: dict[str, dict[str, float | None]] = {}
        rows = MT5HTMLImporter._table_to_rows(orders_table)
        if not rows:
            return mapping

        header = rows[0]
        data_rows = rows[1:]

        col_order_id = MT5HTMLImporter._find_col_index(header, ["Auftrag"])
        col_sl = MT5HTMLImporter._find_col_index(header, ["S / L", "S/L"])
        col_tp = MT5HTMLImporter._find_col_index(header, ["T / P", "T/P"])

        if col_order_id is None:
            return mapping

        # DV-B44: "2.500" alone is ambiguous (2.5 ASCII vs 2500 German), so
        # the report locale is detected from grouped values across the whole
        # table before parsing any single cell.  Only the SL/TP columns are
        # parsed with this locale, but a grouped value anywhere in the table
        # is evidence for it — the XLSX importer must stay in agreement here
        # (DV-B43), so do not narrow the scan to the SL/TP columns only.
        decimal_comma = detect_decimal_comma(
            [row[col] for row in data_rows for col in range(len(row))]
        )

        for row in data_rows:
            if col_order_id >= len(row):
                continue
            oid = row[col_order_id]
            if not oid:
                continue
            sl = row[col_sl] if (col_sl is not None and col_sl < len(row)) else ""
            tp = row[col_tp] if (col_tp is not None and col_tp < len(row)) else ""
            # Unparseable values stay None instead of raising (aborting the
            # whole import) or being silently corrupted.  A non-empty cell
            # that yields None is logged so the skip stays observable.
            parsed_sl = parse_locale_number(sl, decimal_comma)
            parsed_tp = parse_locale_number(tp, decimal_comma)
            if sl and parsed_sl is None:
                logger.warning("Order %s: unparseable S/L value %r", oid, sl)
            if tp and parsed_tp is None:
                logger.warning("Order %s: unparseable T/P value %r", oid, tp)
            mapping[oid] = {
                "sl": parsed_sl,
                "tp": parsed_tp,
            }
        return mapping

    # ── trade reconstruction ──────────────────────────────────────────────

    @staticmethod
    def _build_trades(
        trades_table: BeautifulSoup,
        sltp_map: dict[str, dict[str, float | None]],
        source_timezone: str | None = None,
    ) -> list[Trade]:
        rows = MT5HTMLImporter._table_to_rows(trades_table)
        if not rows:
            return []

        header = rows[0]
        data_rows = rows[1:]

        col_time = MT5HTMLImporter._find_col_index(header, ["Zeit"])
        col_trade_id = MT5HTMLImporter._find_col_index(header, ["Auftrag", "Trade"])
        col_typ = MT5HTMLImporter._find_col_index(header, ["Typ"])
        col_direction = MT5HTMLImporter._find_col_index(header, ["Richtung"])
        col_symbol = MT5HTMLImporter._find_col_index(header, ["Symbol"])
        col_volume = MT5HTMLImporter._find_col_index(header, ["Volumen"])
        col_price = MT5HTMLImporter._find_col_index(header, ["Preis"])
        col_order_id = MT5HTMLImporter._find_col_index(header, ["Auftrag"])
        col_commission = MT5HTMLImporter._find_col_index(header, ["Kommission"])
        col_swap = MT5HTMLImporter._find_col_index(header, ["Swap"])
        col_profit = MT5HTMLImporter._find_col_index(header, ["Gewinn"])
        col_comment = MT5HTMLImporter._find_col_index(header, ["Kommentar"])

        # DV-B42: direction is derived from the Typ column, so it must be
        # present.  Without this check a missing or locale-variant Typ column
        # silently relabels every trade as SHORT.
        if col_time is None or col_direction is None or col_typ is None:
            raise ValueError(
                "Trades table is missing required columns (Zeit / Typ / Richtung)."
            )

        # DV-B43: group all fills per position id (the Trade column; Auftrag
        # order ids differ per fill), then reconstruct one Trade per position
        # with one TradeExit per out row.  The shared helper keeps the HTML
        # and XLSX importers in agreement on the same report.
        col_position_id = MT5HTMLImporter._find_col_index(header, ["Trade", "Auftrag"])

        def _row_direction(row: list[str]) -> str:
            return MT5HTMLImporter._get_str(row, col_direction, "").lower()

        def _row_position_id(row: list[str]) -> str:
            return MT5HTMLImporter._get_str(row, col_position_id, "") if col_position_id is not None else ""

        position_fills = group_position_fills(
            data_rows,
            position_id_of=_row_position_id,
            direction_of=_row_direction,
            is_trade_row=lambda row: MT5HTMLImporter._get_str(row, col_typ, "").lower() != "balance",
        )

        trades: list[Trade] = []
        trade_id_counter = 1

        for fills in closed_positions(position_fills):
            entry_row = fills.entries[0]
            exit_rows = fills.exits

            symbol = MT5HTMLImporter._get_str(entry_row, col_symbol, "")
            entry_time_raw = MT5HTMLImporter._get_str(entry_row, col_time, "")

            entry_price = MT5HTMLImporter._to_float(entry_row, col_price)
            entry_volume = sum(
                (MT5HTMLImporter._to_float(r, col_volume) or 0.0) for r in fills.entries
            )

            typ_entry = MT5HTMLImporter._get_str(entry_row, col_typ, "").lower()
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
                (MT5HTMLImporter._to_float(r, col_commission, default=0.0) or 0.0)
                for r in fills.entries + exit_rows
            )
            swap = sum(
                (MT5HTMLImporter._to_float(r, col_swap, default=0.0) or 0.0)
                for r in fills.entries + exit_rows
            )
            fees = commission + swap

            exits: list[TradeExit] = []
            for exit_row in exit_rows:
                exit_time_raw = MT5HTMLImporter._get_str(exit_row, col_time, "")
                exit_price = MT5HTMLImporter._to_float(exit_row, col_price)
                exit_volume = MT5HTMLImporter._to_float(exit_row, col_volume)
                exit_pnl = MT5HTMLImporter._to_float(exit_row, col_profit)

                exit_comment = MT5HTMLImporter._get_str(exit_row, col_comment, "")
                exit_reason = MT5HTMLImporter._infer_exit_reason(exit_comment)
                exits.append(
                    TradeExit(
                        time=MT5HTMLImporter._to_iso(exit_time_raw, source_timezone),
                        price=exit_price if exit_price is not None else 0.0,
                        quantity=exit_volume if exit_volume is not None else 0.0,
                        pnl=exit_pnl,
                        reason=exit_reason,
                    )
                )
            last_exit_comment = MT5HTMLImporter._get_str(exit_rows[-1], col_comment, "")
            exit_reason = MT5HTMLImporter._infer_exit_reason(last_exit_comment)

            pnl = sum((e.pnl or 0.0) for e in exits)

            entry_comment = MT5HTMLImporter._get_str(entry_row, col_comment, "")
            tags = [entry_comment] if entry_comment else []

            order_id = MT5HTMLImporter._get_str(entry_row, col_order_id, "")
            sl = tp = None
            if order_id:
                mapped = sltp_map.get(order_id, {})
                sl = mapped.get("sl")
                tp = mapped.get("tp")

            trade = Trade(
                id=trade_id_counter,
                symbol=symbol,
                direction=direction_str,
                entry_time=MT5HTMLImporter._to_iso(entry_time_raw, source_timezone),
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
                    "order_id": order_id if order_id else None,
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
    def _find_col_index(header: list[str], candidates: list[str]) -> int | None:
        for cand in candidates:
            for idx, h in enumerate(header):
                if h == cand or str(h).strip() == cand:
                    return idx
        return None

    @staticmethod
    def _get_str(row: list[str], col: int | None, default: str) -> str:
        if col is None or col >= len(row):
            return default
        val = row[col]
        return str(val).strip() if val is not None else default

    @staticmethod
    def _to_float(row: list[str], col: int | None, default: float | None = None) -> float | None:
        if col is None or col >= len(row):
            return default
        val = row[col]
        if not val:
            return default
        parsed = parse_locale_number(val, decimal_comma=False)
        return default if parsed is None else parsed

    @staticmethod
    def _to_iso(raw: str, source_tz: str | None = None) -> str:
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


def _parse_german_number(raw: str | None) -> float | None:
    """Parse a number using the German convention (dot = thousands group)."""
    from validation.importers._numbers import parse_locale_number

    return parse_locale_number(raw, decimal_comma=True)


__all__ = ["MT5HTMLImporter", "_parse_german_number"]
