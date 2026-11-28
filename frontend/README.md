# frontend

React + TypeScript + Vite SPA for the trading-platform viewer. Served in
production by the FastAPI backend (`viewer/main.py`) from `frontend/dist`;
in development Vite runs on :3000 and proxies `/api` to the backend on :5556.

## Commands

```bash
npm install
npm run dev        # Vite dev server (:3000)
npm run build      # tsc -b && vite build -> dist/
npm run lint       # eslint
npm run test:focused          # node --test suite for core logic
npm run test:optimization-evidence
```

## Layout

- `src/App.tsx` — route registry; `src/Sidebar.tsx` — navigation
- `src/pages/` — one component per page (backtests, strategies, portfolios,
  live trading hub/detail/viewer, data management, strategy lab, ...)
- `src/api/client.ts` + `src/api/types.ts` — mirror the FastAPI endpoints in
  `viewer/main.py`; keep them in sync when backend fields change
- `src/components/TradingViewChart.tsx` — Lightweight Charts wrapper

Stack: React 19, react-router, TanStack Query, Tailwind v4, AG Grid,
Recharts, Lightweight Charts, Sentry.
