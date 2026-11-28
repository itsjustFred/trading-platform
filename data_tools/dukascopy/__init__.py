"""Dukascopy data: the on-demand downloader plus the incremental dataset pipeline.

``native`` is the interactive downloader used by the viewer, auto-download and
the discovery/grid scripts (bi5 fetch, tick/candle decode, catalog writes).
``pipeline`` maintains the canonical two-sided dataset layout
(``<SYM>/<SYM>_<TF>.parquet`` + ``manifest.json``) that mirrors the R2 bucket.
"""
