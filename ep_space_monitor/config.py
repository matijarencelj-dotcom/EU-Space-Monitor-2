"""Configuration loading (config.toml + keywords.toml)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .detection import KeywordSet, load_keywords


@dataclass(frozen=True)
class MonitorConfig:
    committees: tuple[str, ...]
    language: str
    agenda_lookback_days: int
    agenda_lookahead_days: int
    pending_max_age_days: int
    request_delay_seconds: float
    request_timeout_seconds: int
    max_retries: int
    first_run_backfill: bool
    backfill_months: int
    questions_enabled: bool
    questions_lookback_days: int
    questions_first_run_days: int
    questions_max_new: int
    questions_page_size: int
    questions_max_pages: int
    plenary_enabled: bool
    plenary_lookback_days: int
    plenary_queries: tuple[str, ...]
    plenary_limit_per_query: int
    eurlex_enabled: bool
    eurlex_lookback_days: int
    eurlex_queries: tuple[str, ...]
    eurlex_limit: int
    ft_enabled: bool
    ft_lookback_days: int
    ft_queries: tuple[str, ...]
    ft_page_size: int


@dataclass(frozen=True)
class NetworkConfig:
    allowlist: tuple[str, ...]
    user_agent: str


@dataclass(frozen=True)
class EmailConfig:
    enabled: bool
    to: tuple[str, ...]
    sender: str
    subject_prefix: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password_env: str

    def password(self) -> str | None:
        return os.environ.get(self.smtp_password_env) or None


@dataclass(frozen=True)
class DashboardConfig:
    output_path: str
    site_name: str


@dataclass(frozen=True)
class Config:
    root: Path
    monitor: MonitorConfig
    network: NetworkConfig
    email: EmailConfig
    dashboard: DashboardConfig
    keywords: KeywordSet
    data_dir: Path
    state_path: Path
    findings_path: Path
    dashboard_path: Path


def load_config(path: str | Path = "config.toml") -> Config:
    """Load the full configuration from a TOML file (plus its keyword file)."""
    config_path = Path(path).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    root = config_path.parent

    general: dict[str, Any] = raw.get("general", {})
    monitor: dict[str, Any] = raw.get("monitor", {})
    network: dict[str, Any] = raw.get("network", {})
    email: dict[str, Any] = raw.get("email", {})
    dash: dict[str, Any] = raw.get("dashboard", {})

    data_dir = Path(general.get("data_dir", "data"))
    if not data_dir.is_absolute():
        data_dir = root / data_dir

    keywords_file = Path(general.get("keywords_file", "keywords.toml"))
    if not keywords_file.is_absolute():
        keywords_file = root / keywords_file
    keywords = load_keywords(keywords_file)

    monitor_cfg = MonitorConfig(
        committees=tuple(monitor.get("committees", ["ITRE"])),
        language=general.get("language", "EN"),
        agenda_lookback_days=int(monitor.get("agenda_lookback_days", 21)),
        agenda_lookahead_days=int(monitor.get("agenda_lookahead_days", 21)),
        pending_max_age_days=int(monitor.get("pending_max_age_days", 30)),
        request_delay_seconds=float(monitor.get("request_delay_seconds", 0.25)),
        request_timeout_seconds=int(monitor.get("request_timeout_seconds", 30)),
        max_retries=int(monitor.get("max_retries", 2)),
        first_run_backfill=bool(monitor.get("first_run_backfill", True)),
        backfill_months=int(monitor.get("backfill_months", 6)),
        questions_enabled=bool(monitor.get("questions_enabled", True)),
        questions_lookback_days=int(monitor.get("questions_lookback_days", 30)),
        questions_first_run_days=int(monitor.get("questions_first_run_days", 180)),
        questions_max_new=int(monitor.get("questions_max_new", 400)),
        questions_page_size=int(monitor.get("questions_page_size", 100)),
        questions_max_pages=int(monitor.get("questions_max_pages", 60)),
        plenary_enabled=bool(monitor.get("plenary_enabled", True)),
        plenary_lookback_days=int(monitor.get("plenary_lookback_days", 14)),
        plenary_queries=tuple(monitor.get("plenary_queries", ["space", "galileo", "copernicus"])),
        plenary_limit_per_query=int(monitor.get("plenary_limit_per_query", 25)),
        eurlex_enabled=bool(monitor.get("eurlex_enabled", True)),
        eurlex_lookback_days=int(monitor.get("eurlex_lookback_days", 14)),
        eurlex_queries=tuple(monitor.get("eurlex_queries", ["space", "galileo", "copernicus"])),
        eurlex_limit=int(monitor.get("eurlex_limit", 100)),
        ft_enabled=bool(monitor.get("ft_enabled", True)),
        ft_lookback_days=int(monitor.get("ft_lookback_days", 30)),
        ft_queries=tuple(monitor.get("ft_queries", ["space", "galileo", "copernicus"])),
        ft_page_size=int(monitor.get("ft_page_size", 100)),
    )

    dashboard_cfg = DashboardConfig(
        output_path=dash.get("output_path", "data/dashboard.html"),
        site_name=dash.get("site_name", "EP Space Monitor"),
    )

    return Config(
        root=root,
        monitor=monitor_cfg,
        network=NetworkConfig(
            allowlist=tuple(
                network.get(
                    "allowlist",
                    ["emeeting.europarl.europa.eu", "data.europarl.europa.eu", "publications.europa.eu",
                     "api.tech.ec.europa.eu"],
                )
            ),
            user_agent=network.get(
                "user_agent",
                "ESPISpaceMonitor/2.0 (https://espi.eu)",
            ),
        ),
        email=EmailConfig(
            enabled=bool(email.get("enabled", False)),
            to=tuple(email.get("to", [])),
            sender=email.get("from", "ep-space-monitor@espi.eu"),
            subject_prefix=email.get("subject_prefix", "EP Space Monitor"),
            smtp_host=email.get("smtp_host", "smtp-relay.brevo.com"),
            smtp_port=int(email.get("smtp_port", 587)),
            smtp_user=email.get("smtp_user", ""),
            smtp_password_env=email.get("smtp_password_env", "EP_SPACE_MONITOR_SMTP_PASSWORD"),
        ),
        dashboard=dashboard_cfg,
        keywords=keywords,
        data_dir=data_dir,
        state_path=data_dir / "state.json",
        findings_path=data_dir / "findings.jsonl",
        dashboard_path=root / dashboard_cfg.output_path if not Path(dashboard_cfg.output_path).is_absolute() else Path(dashboard_cfg.output_path),
    )
