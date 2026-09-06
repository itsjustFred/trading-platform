import { useMemo } from "react";
import type { ColDef, RowClickedEvent, ValueFormatterParams } from "ag-grid-community";
import { AllCommunityModule } from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import "ag-grid-community/styles/ag-grid.css";
import "ag-grid-community/styles/ag-theme-alpine.css";

export interface TradeTableRow {
  id: string;
  date?: string;
  time: string;
  strategy?: string;
  symbol: string;
  side: string;
  signalSource?: string | null;
  signalTime?: string | null;
  fillLagSec?: number | null;
  size?: number | null;
  entryPrice?: number | null;
  exitPrice?: number | null;
  processorStopLoss?: number | null;
  processorTakeProfit?: number | null;
  pnl?: number | null;
  pnlPct?: number | null;
  exitReason?: string | null;
  duration?: string | null;
}

interface TradeTableProps {
  trades: TradeTableRow[];
  onTradeClick?: (trade: TradeTableRow) => void;
  showSignalColumns?: boolean;
}

function toFiniteNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return null;
}

function formatTimestamp(value: string): string {
  if (!value) return "—";
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) {
    return value;
  }
  return new Date(parsed).toISOString().slice(0, 19).replace("T", " ");
}

function formatDate(value: string): string {
  if (!value) return "—";
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) {
    return value;
  }
  return new Date(parsed).toISOString().slice(0, 10);
}

function formatNumber(value: unknown, digits = 2): string {
  const numeric = toFiniteNumber(value);
  if (numeric == null) return "—";
  return numeric.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function formatPrice(value: unknown): string {
  const numeric = toFiniteNumber(value);
  if (numeric == null) return "—";
  return `$${numeric.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 5,
  })}`;
}

function formatPnl(value: unknown): string {
  const numeric = toFiniteNumber(value);
  if (numeric == null) return "—";
  return `${numeric >= 0 ? "+" : "-"}$${Math.abs(numeric).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function formatPercent(value: unknown): string {
  const numeric = toFiniteNumber(value);
  if (numeric == null) return "—";
  return `${numeric >= 0 ? "+" : ""}${numeric.toFixed(2)}%`;
}

function formatLag(value: unknown): string {
  const numeric = toFiniteNumber(value);
  if (numeric == null || numeric < 0) return "—";
  const seconds = Math.trunc(numeric);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.trunc(seconds / 60);
  const rest = seconds % 60;
  if (minutes < 60) return `${minutes}m ${rest}s`;
  const hours = Math.trunc(minutes / 60);
  const restMinutes = minutes % 60;
  return `${hours}h ${restMinutes}m`;
}

const pnlCellStyle = (value: unknown) => {
  const numeric = toFiniteNumber(value);
  if (numeric == null || numeric === 0) {
    return undefined;
  }
  return {
    color: numeric > 0 ? "#3fb950" : "#f85149",
    fontWeight: 700,
  };
};

export function TradeTable({
  trades,
  onTradeClick,
  showSignalColumns = false,
}: Readonly<TradeTableProps>) {
  const columnDefs = useMemo<ColDef<TradeTableRow>[]>(
    () => {
      const signalColumns: ColDef<TradeTableRow>[] = showSignalColumns
        ? [
            {
              field: "signalSource",
              headerName: "Signal Source",
              minWidth: 150,
              flex: 1.1,
              valueFormatter: (params: ValueFormatterParams<TradeTableRow>) =>
                String(params.value ?? "").trim() || "—",
            },
            {
              field: "signalTime",
              headerName: "Signal Time",
              minWidth: 150,
              flex: 1.05,
              valueFormatter: (params: ValueFormatterParams<TradeTableRow>) =>
                formatTimestamp(String(params.value ?? "")),
            },
            {
              field: "fillLagSec",
              headerName: "Fill Lag",
              minWidth: 105,
              flex: 0.8,
              type: "numericColumn",
              valueFormatter: (params: ValueFormatterParams<TradeTableRow>) =>
                formatLag(params.value),
            },
          ]
        : [];

      return [
        {
          field: "date",
          headerName: "Date",
          minWidth: 120,
          flex: 0.8,
          valueFormatter: (params: ValueFormatterParams<TradeTableRow>) =>
            formatDate(String(params.value ?? "")),
        },
        {
          field: "time",
          headerName: "Time",
          minWidth: 110,
          flex: 0.8,
          valueFormatter: (params: ValueFormatterParams<TradeTableRow>) =>
            formatTimestamp(String(params.value ?? "")),
        },
        {
          field: "strategy",
          headerName: "Strategy",
          minWidth: 220,
          flex: 1.4,
          valueFormatter: (params: ValueFormatterParams<TradeTableRow>) =>
            String(params.value ?? "").trim() || "—",
        },
        {
          field: "symbol",
          headerName: "Symbol",
          minWidth: 110,
          flex: 0.8,
          valueFormatter: (params: ValueFormatterParams<TradeTableRow>) =>
            String(params.value ?? "").toUpperCase(),
        },
        {
          field: "side",
          headerName: "Side",
          minWidth: 100,
          flex: 0.7,
          valueFormatter: (params: ValueFormatterParams<TradeTableRow>) =>
            String(params.value ?? "").toUpperCase(),
        },
        ...signalColumns,
        {
          field: "size",
          headerName: "Size",
          minWidth: 120,
          flex: 0.9,
          type: "numericColumn",
          valueFormatter: (params: ValueFormatterParams<TradeTableRow>) =>
            formatNumber(params.value, 4),
        },
        {
          field: "entryPrice",
          headerName: "Entry Price",
          minWidth: 130,
          flex: 0.95,
          type: "numericColumn",
          valueFormatter: (params: ValueFormatterParams<TradeTableRow>) =>
            formatPrice(params.value),
        },
        {
          field: "exitPrice",
          headerName: "Exit Price",
          minWidth: 130,
          flex: 0.95,
          type: "numericColumn",
          valueFormatter: (params: ValueFormatterParams<TradeTableRow>) =>
            formatPrice(params.value),
        },
        {
          field: "processorStopLoss",
          headerName: "Signal SL",
          minWidth: 120,
          flex: 0.9,
          type: "numericColumn",
          valueFormatter: (params: ValueFormatterParams<TradeTableRow>) =>
            formatPrice(params.value),
        },
        {
          field: "processorTakeProfit",
          headerName: "Signal TP",
          minWidth: 120,
          flex: 0.9,
          type: "numericColumn",
          valueFormatter: (params: ValueFormatterParams<TradeTableRow>) =>
            formatPrice(params.value),
        },
        {
          field: "exitReason",
          headerName: "Exit Reason",
          minWidth: 180,
          flex: 1.2,
          valueFormatter: (params: ValueFormatterParams<TradeTableRow>) =>
            String(params.value ?? "").trim() || "—",
        },
        {
          field: "duration",
          headerName: "Duration",
          minWidth: 120,
          flex: 0.9,
          valueFormatter: (params: ValueFormatterParams<TradeTableRow>) =>
            String(params.value ?? "").trim() || "—",
        },
        {
          field: "pnl",
          headerName: "P&L",
          minWidth: 120,
          flex: 0.95,
          type: "numericColumn",
          valueFormatter: (params: ValueFormatterParams<TradeTableRow>) =>
            formatPnl(params.value),
          cellStyle: (params) => pnlCellStyle(params.value),
        },
        {
          field: "pnlPct",
          headerName: "P&L%",
          minWidth: 105,
          flex: 0.8,
          type: "numericColumn",
          valueFormatter: (params: ValueFormatterParams<TradeTableRow>) =>
            formatPercent(params.value),
          cellStyle: (params) => pnlCellStyle(params.value),
        },
      ];
    },
    [showSignalColumns],
  );

  return (
    <div
      className="ag-theme-alpine-dark"
      style={{ width: "100%", height: "560px", borderRadius: "8px", overflow: "hidden" }}
    >
      <AgGridReact<TradeTableRow>
        modules={[AllCommunityModule]}
        theme="legacy"
        rowData={trades}
        columnDefs={columnDefs}
        defaultColDef={{
          sortable: true,
          resizable: true,
          filter: false,
          suppressHeaderMenuButton: true,
        }}
        animateRows
        rowHeight={36}
        pagination
        paginationPageSize={50}
        paginationPageSizeSelector={[25, 50, 100, 200]}
        onRowClicked={(event: RowClickedEvent<TradeTableRow>) => {
          if (!event.data || !onTradeClick) return;
          onTradeClick(event.data);
        }}
        getRowId={(params) => params.data.id}
        suppressMovableColumns
      />
    </div>
  );
}
