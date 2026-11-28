"""CLI for the Dukascopy dataset pipeline.

    python -m data_tools.dukascopy update --root /path/to/dukascopy [--upload]
    python -m data_tools.dukascopy download --symbols EURUSD --timeframes d1 ...

``update`` runs the incremental (or ``--full``) pipeline and optionally
mirrors the changed files to R2. It installs the shared ops alert sink so a
failure or incomplete fetch reaches the configured Discord webhook
(``OPS_ALERT_WEBHOOK_URL``); the sink is a no-op when unset.
"""

from __future__ import annotations

import argparse
import logging
import sys


def _alert_sink():
    try:
        from shared.alerts import install_alert_sink
    except Exception:  # noqa: BLE001 - alerting must never break the pipeline
        return None
    try:
        return install_alert_sink()
    except Exception:  # noqa: BLE001
        return None


def _close_alert_sink() -> None:
    try:
        from shared.alerts import close_alert_sink

        close_alert_sink()
    except Exception:  # noqa: BLE001 - alerting must never break the pipeline
        logging.getLogger(__name__).debug("alert sink close failed", exc_info=True)


def _emit_alert(message: str, **extra) -> None:
    # shared.logging.get_logger yields a ``trading.*`` logger, which is what
    # the ops alert sink (Discord webhook) is attached to.
    from shared.logging import get_logger

    get_logger("dukascopy").error(
        message, extra={"data": {"alert": "dukascopy_update_failed", **extra}}
    )


def cmd_update(args: argparse.Namespace) -> int:
    # Load .env the way the platform does before reading alert/upload config.
    import shared.constants  # noqa: F401
    from data_tools.dukascopy import pipeline, upload

    _alert_sink()
    symbols = (
        [s.strip() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else None
    )
    try:
        report = pipeline.update_dataset(
            args.root,
            symbols=symbols,
            full=args.full,
            start=args.start,
            end=args.end,
            raw_dir=args.raw_dir,
            concurrency=args.concurrency,
        )
        problems = pipeline.verify_manifest(args.root)
        if problems:
            _emit_alert(
                "Dukascopy manifest inconsistent after publish",
                problems=problems[:10],
            )
            print(f"MANIFEST_INCONSISTENT: {problems[:5]}", file=sys.stderr)
            return 1
        if args.upload:
            uploaded = upload.upload_dataset(args.root)
            print(f"uploaded {len(uploaded)} objects to R2")
        for s in report.symbols:
            print(f"{s.symbol}: {s.status} (+{s.new_m1_rows} M1 rows)")
        return 0
    except pipeline.IncompleteFetchError as exc:
        _emit_alert(f"Dukascopy update incomplete: {exc}")
        print(f"INCOMPLETE: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report and exit non-zero
        _emit_alert(f"Dukascopy update failed: {exc}")
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        _close_alert_sink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m data_tools.dukascopy")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("update", help="Incremental dataset update")
    up.add_argument("--root", required=True, help="Dataset root (contains manifest.json)")
    up.add_argument("--symbols", help="Comma-separated canonical names or Dukascopy codes")
    up.add_argument("--full", action="store_true", help="Rebuild all history from 2015-01-01")
    up.add_argument("--start", help="Fetch start date YYYY-MM-DD (default: manifest last)")
    up.add_argument(
        "--end", help="Fetch end date YYYY-MM-DD, exclusive (default: today UTC)"
    )
    up.add_argument("--raw-dir", help="bi5 cache dir (default: <root>/_raw)")
    up.add_argument("--concurrency", type=int, default=8)
    up.add_argument(
        "--upload", action="store_true",
        help="Mirror changed files to R2 after publish",
    )
    up.set_defaults(func=cmd_update)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "download":
        # Reuse the native downloader CLI unchanged; its options are not
        # declared here, so dispatch before argparse sees them.
        sys.argv = ["dukascopy-native", *argv[1:]]
        from data_tools.dukascopy import native

        native.main()
        return 0
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
