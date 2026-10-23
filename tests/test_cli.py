"""CLI argument-parsing tests.

Regression: ``--config`` used to be accepted only *before* the subcommand
(``ep-space-monitor --config x run``), while the workflow invokes it after
(``ep-space-monitor run --config config.toml``) — argparse exited with
code 2 and the daily run died instantly. The option now lives on every
subcommand.
"""

from ep_space_monitor.cli import build_parser


def test_config_option_after_subcommand():
    args = build_parser().parse_args(
        ["run", "--config", "config.toml", "--backfill-months", "0"]
    )
    assert args.command == "run"
    assert args.config == "config.toml"
    assert args.backfill_months == 0
    assert args.dry_run is False


def test_run_with_defaults():
    args = build_parser().parse_args(["run"])
    assert args.command == "run"
    assert args.config == "config.toml"
    assert args.backfill_months is None


def test_backfill_options():
    args = build_parser().parse_args(
        ["backfill", "--months", "6", "--dry-run", "--config", "other.toml"]
    )
    assert args.command == "backfill"
    assert args.months == 6
    assert args.dry_run is True
    assert args.config == "other.toml"


def test_dashboard_and_test_email_parsers():
    args = build_parser().parse_args(["dashboard"])
    assert args.command == "dashboard"
    assert args.config == "config.toml"

    args = build_parser().parse_args(["test-email", "--to", "someone@example.org"])
    assert args.command == "test-email"
    assert args.to == "someone@example.org"
    assert args.config == "config.toml"
