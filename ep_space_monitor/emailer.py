"""E-mail alerts with new findings, via SMTP (e.g. the EU provider Brevo).

Renders a short ESPI-styled HTML message plus a plain-text fallback and
sends it through the configured SMTP relay. The SMTP password is read
from an environment variable, never from the config file.
"""

from __future__ import annotations

import html
import smtplib
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .config import EmailConfig
from .state import Finding

EMAIL_CSS = (
    "body{background:#0C1112;color:#EAE6E9;font-family:Arial,Helvetica,sans-serif;margin:0;padding:24px;}"
    ".wrap{max-width:640px;margin:0 auto;}"
    "h1{font-family:Arial,sans-serif;font-size:20px;letter-spacing:1px;text-transform:uppercase;}"
    ".wm{color:#8ACCD0;font-weight:bold;font-size:22px;}"
    ".accent{color:#E82275;}"
    ".meta{color:#9A9298;font-size:12px;margin:6px 0 18px;}"
    ".card{background:#240D16;border-left:3px solid #E82275;padding:12px 14px;margin:10px 0;}"
    ".tag{display:inline-block;font-size:10px;font-weight:bold;padding:1px 6px;margin-right:6px;border-radius:2px;color:#0C1112;background:#8ACCD0;}"
    ".com{display:inline-block;font-size:10px;font-weight:bold;padding:1px 8px;border-radius:9px;border:1px solid #8ACCD0;color:#8ACCD0;letter-spacing:1px;}"
    ".title{font-size:14px;margin:6px 0 4px;font-weight:bold;}"
    ".kw{display:inline-block;font-size:11px;color:#E82275;border:1px solid #E82275;border-radius:9px;padding:0 7px;margin:2px 3px 2px 0;}"
    ".excerpt{color:#9A9298;font-size:12px;font-style:italic;margin:6px 0;}"
    "a{color:#8ACCD0;font-size:12px;}"
    ".footer{color:#9A9298;font-size:11px;margin-top:20px;border-top:1px solid #240D16;padding-top:10px;}"
)

MAX_ITEMS_IN_EMAIL = 25

# Label shown per finding kind, in the e-mail cards and the plain text.
KIND_TAGS = {
    "OJ": "AGENDA (heads-up)",
    "PQ": "QUESTION",
    "PS": "PLENARY",
    "LX": "EUR-LEX",
    "FT": "FUNDING",
    "PV": "MINUTES",
}


def kind_tag(kind: str) -> str:
    return KIND_TAGS.get(kind, kind.upper())


def build_message(findings: list[Finding], run_date: str, email_config: EmailConfig) -> tuple[str, str, str]:
    """Return (subject, html_body, plain_text_body)."""
    shown = findings[:MAX_ITEMS_IN_EMAIL]
    hidden = len(findings) - len(shown)

    subject = (
        f"{email_config.subject_prefix}: {len(findings)} new space-relevant item(s) — {run_date}"
        if findings
        else f"{email_config.subject_prefix}: no new space-relevant items — {run_date}"
    )

    cards_html = []
    for f in shown:
        kws = "".join(f'<span class="kw">{html.escape(t)}</span>' for t in f.matched)
        cards_html.append(
            f'<div class="card">'
            f'<span class="tag">{kind_tag(f.kind)}</span><span class="com">{html.escape(f.committee.upper())}</span>'
            f" · <small>{html.escape(f.date)} · {'item ' + str(f.item_index) + ' · ' if f.kind == 'OJ' else ''}score {f.score:.1f}</small>"
            f'<div class="title">{html.escape(f.title)}</div>'
            f'<div>{kws}</div>'
            f'<div class="excerpt">{html.escape(f.excerpt)}</div>'
            f'<a href="{html.escape(f.url, quote=True)}">Open source document</a>'
            f"</div>"
        )
    if hidden > 0:
        cards_html.append(
            f'<p class="excerpt">… and {hidden} more item(s) in the dashboard.</p>'
        )
    if not shown:
        cards_html.append('<p class="excerpt">Nothing new since the last run.</p>')

    html_body = (
        f'<div class="wrap">'
        f'<span class="wm">ESPI</span> <span class="accent">//</span> <h1 style="display:inline">EP Space Monitor</h1>'
        f'<div class="meta">Run of {html.escape(run_date)} · {len(findings)} new flagged item(s)</div>'
        + "".join(cards_html)
        + f'<div class="footer">EP Space Monitor — European Space Policy Institute · '
        f"flags European Parliament agendas, questions and plenary debates on the EU space sector "
        f"(EU Space Programme, Galileo, Copernicus, IRIS², EU Space Act, launchers, space defence, ESA, EUSPA).</div>"
        f"</div>"
    )
    html_full = f"<html><body style=\"background:#0C1112;margin:0;padding:24px;\">{html_body}</body></html>"

    text_lines = [f"EP SPACE MONITOR — run of {run_date}", f"{len(findings)} new space-relevant item(s)", ""]
    for f in shown:
        location = f"item {f.item_index}" if f.kind == "OJ" else "EP"
        text_lines += [
            f"[{kind_tag(f.kind)}] {f.committee.upper()} {f.date} — {location} (score {f.score:.1f})",
            f.title,
            f"Keywords: {', '.join(f.matched)}",
            f"Excerpt: {f.excerpt}",
            f"Source: {f.url}",
            "",
        ]
    if hidden > 0:
        text_lines.append(f"… and {hidden} more item(s) in the dashboard.")
    plain = "\n".join(text_lines)

    return subject, html_full, plain


@dataclass
class Mailer:
    """SMTP mailer (Brevo-compatible: STARTTLS on port 587)."""

    config: EmailConfig

    def send(self, subject: str, html_body: str, plain_body: str) -> None:
        password = self.config.password()
        if not password:
            raise RuntimeError(
                f"SMTP password not set: provide it via the "
                f"{self.config.smtp_password_env} environment variable."
            )
        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = self.config.sender
        message["To"] = ", ".join(self.config.to)
        message.attach(MIMEText(plain_body, "plain", "utf-8"))
        message.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            if self.config.smtp_user:
                smtp.login(self.config.smtp_user, password)
            smtp.sendmail(self.config.sender, list(self.config.to), message.as_string())

    def send_findings(self, findings: list[Finding], run_date: str) -> None:
        subject, html_body, plain = build_message(findings, run_date, self.config)
        self.send(subject, html_body, plain)
