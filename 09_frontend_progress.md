# 09 Frontend Progress

## Backend-Fixes

- Vectorbt-Execution wurde auf **Next-Open** umgestellt: `entries`/`exits` werden intern um 1 Bar verschoben, Orders werden standardmäßig zum `open` der Ausführungs-Kerze bewertet, und Stop-/Take-Profit-Metadaten bleiben auf den Signal-Bar ausgerichtet.
