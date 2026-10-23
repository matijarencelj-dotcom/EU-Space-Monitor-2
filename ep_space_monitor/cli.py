"""Command-line interface for the EP Space Monitor."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import load_config
from .runner import EPCommitteeMonitor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ep-space-monitor",
        description="ESPI EP Space Monitor — tracks European Parliament committee activity on the EU space sector.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # --config belongs to each subcommand so the natural ordering
    # "ep-space-monitor run --config config.toml" is accepted.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="config.toml", help="Path to the TOML configuration file (default: config.toml)")

    run_p = sub.add_parser("run", parents=[common], help="Daily monitoring pass (auto-backfills on first run)")
    run_p.add_argument("--dry-run", action="store_true", help="No e-mail alerts; still update state/dashboard")
    run_p.add_argument(
        "--backfill-months",
        type=int,
        default=None,
        help="Force a backfill horizon in months (0 = never, default: auto on first run)",
    )

    backfill_p = sub.add_parser("backfill", parents=[common], help="Explicit backfill over the last N months")
    backfill_p.add_argument("--months", type=int, default=None, help="Months to backfill (default: from config)")
    backfill_p.add_argument("--dry-run", action="store_true", help="No e-mail alerts")

    sub.add_parser("dashboard", parents=[common], help="Regenerate the dashboard from existing findings only")

    test_p = sub.add_parser("test-email", parents=[common], help="Send a test e-mail to verify SMTP settings")
    test_p.add_argument("--to", default=None, help="Override recipient (default: from config)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "test-email":
        from dataclasses import replace

        from .emailer import Mailer
        from .state import Finding

        email_config = config.email
        if args.to:
            email_config = replace(email_config, to=(args.to,))
        mailer = Mailer(email_config)
        sample = Finding(
            id="TEST#item1", committee="ITRE", kind="OJ", date="1970-01-01",
            doc_number=1, url="https://www.europarl.europa.eu/", item_index=1,
            title="Test e-mail from EP Space Monitor",
            excerpt="If you can read this, your SMTP settings work.",
            score=99.0, matched={"test": 99.0}, retrieved_at="now",
        )
        mailer.send_findings([sample], "TEST")
        print(f"Test e-mail sent to {', '.join(email_config.to)}.")
        return 0

    monitor = EPCommitteeMonitor(config)

    if args.command == "dashboard":
        monitor.write_dashboard()
        print(f"Dashboard written to {config.dashboard_path}.")
        return 0

    if args.command == "backfill":
        months = args.months if args.months is not None else config.monitor.backfill_months
        if months <= 0:
            print("error: --months must be positive", file=sys.stderr)
            return 2
        result = monitor.backfill(months, dry_run=args.dry_run)
    else:  # run
        result = monitor.run(dry_run=args.dry_run, backfill_months=args.backfill_months)

    print(result.summary())
    for finding in result.new_findings:
        print(f"  * [{finding.kind_label}] {finding.committee} {finding.date} — {finding.title}")
    if result.errors:
        print(f"Completed with {len(result.errors)} error(s).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
