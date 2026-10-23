"""E-mail rendering and SMTP sending tests."""

import pytest

from ep_space_monitor.config import EmailConfig
from ep_space_monitor.emailer import Mailer, build_message
from ep_space_monitor.state import Finding


def email_config(**overrides):
    defaults = dict(
        enabled=True,
        to=("matija.rencelj@espi.eu",),
        sender="ep-space-monitor@espi.eu",
        subject_prefix="EP Space Monitor",
        smtp_host="smtp-relay.brevo.com",
        smtp_port=587,
        smtp_user="",
        smtp_password_env="EP_SPACE_MONITOR_SMTP_PASSWORD",
    )
    defaults.update(overrides)
    return EmailConfig(**defaults)


def make_finding(fid, title="Exchange of views on the EU Space Programme"):
    return Finding(
        id=fid, committee="ITRE", kind="OJ", date="2026-10-21",
        doc_number=1, url="https://www.europarl.europa.eu/doceo/document/x.html",
        item_index=6, title=title, excerpt="…EU Space Programme and Galileo…",
        score=7.0, matched={"eu space programme": 3.5, "galileo": 3.0},
        retrieved_at="2026-10-22T00:00:00+00:00",
    )


def test_subject_counts_findings():
    subject, _html, _plain = build_message([make_finding("a"), make_finding("b")], "2026-10-22", email_config())
    assert subject == "EP Space Monitor: 2 new space-relevant item(s) — 2026-10-22"


def test_html_and_plain_bodies_contain_findings():
    _s, html_body, plain = build_message([make_finding("a")], "2026-10-22", email_config())
    assert "EU Space Programme" in html_body
    assert "https://www.europarl.europa.eu/doceo/document/x.html" in html_body
    assert "galileo" in html_body  # matched keywords listed
    assert "AGENDA (heads-up)" in html_body
    assert "EU Space Programme" in plain
    assert "Source: https://www.europarl.europa.eu" in plain


def test_minutes_findings_labelled():
    f = make_finding("a")
    finding = Finding(
        id=f.id, committee=f.committee, kind="PV", date=f.date, doc_number=f.doc_number,
        url=f.url, item_index=f.item_index, title=f.title, excerpt=f.excerpt,
        score=f.score, matched=f.matched, retrieved_at=f.retrieved_at,
    )
    _s, html_body, _plain = build_message([finding], "2026-10-22", email_config())
    assert "MINUTES" in html_body


def test_email_caps_listing_at_25_items():
    findings = [make_finding(f"f{i}") for i in range(30)]
    _s, html_body, plain = build_message(findings, "2026-10-22", email_config())
    assert "and 5 more" in html_body
    assert "and 5 more" in plain


def test_no_findings_message():
    _s, html_body, plain = build_message([], "2026-10-22", email_config())
    assert "no new space-relevant items" in _s.lower()
    assert "Nothing new" in html_body


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.tls = False
        self.sent = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.login_args = (user, password)

    def sendmail(self, sender, recipients, body):
        self.sent = (sender, recipients, body)


def test_send_uses_starttls_and_recipients(monkeypatch):
    monkeypatch.setenv("EP_SPACE_MONITOR_SMTP_PASSWORD", "secret-key")
    monkeypatch.setattr("ep_space_monitor.emailer.smtplib.SMTP", FakeSMTP)
    FakeSMTP.instances = []

    mailer = Mailer(email_config(smtp_user="smtp-user@example.com"))
    mailer.send_findings([make_finding("a")], "2026-10-22")

    (smtp,) = FakeSMTP.instances
    assert smtp.host == "smtp-relay.brevo.com"
    assert smtp.port == 587
    assert smtp.tls is True
    assert smtp.login_args == ("smtp-user@example.com", "secret-key")
    sender, recipients, body = smtp.sent
    assert sender == "ep-space-monitor@espi.eu"
    assert recipients == ["matija.rencelj@espi.eu"]
    assert "multipart" in body or "EU Space Programme" in body


def test_send_without_password_raises(monkeypatch):
    monkeypatch.delenv("EP_SPACE_MONITOR_SMTP_PASSWORD", raising=False)
    mailer = Mailer(email_config())
    with pytest.raises(RuntimeError, match="EP_SPACE_MONITOR_SMTP_PASSWORD"):
        mailer.send("s", "<p>h</p>", "p")
