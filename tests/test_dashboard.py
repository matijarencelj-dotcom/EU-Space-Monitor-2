"""Dashboard rendering tests (ESPI branding, grouping, escaping)."""

from ep_space_monitor.dashboard import render_dashboard, write_dashboard
from ep_space_monitor.state import Finding


def make_finding(fid, day, committee="ITRE", title="Exchange of views on the EU Space Programme", **overrides):
    defaults = dict(
        id=fid, committee=committee, kind="OJ", date=day, doc_number=1,
        url="https://www.europarl.europa.eu/doceo/document/x.html",
        item_index=6, title=title, excerpt="…EU Space Programme…",
        score=6.0, matched={"eu space programme": 3.5, "euspa": 3.5},
        retrieved_at="2026-10-22T00:00:00+00:00",
    )
    defaults.update(overrides)
    return Finding(**defaults)


def test_branding_colors_present():
    html = render_dashboard([make_finding("a", "2026-10-21")], "EP Space Monitor")
    assert "#E82275" in html  # Razzmatazz
    assert "#8ACCD0" in html  # Aquamarine
    assert "#0C1112" in html  # Woodsmoke background
    assert "Raleway" in html
    assert "Gugi" in html
    assert "ESPI" in html


def test_committee_stream_colors():
    findings = [
        make_finding("a", "2026-10-21", committee="SEDE"),
        make_finding("b", "2026-10-21", committee="ITRE"),
        make_finding("c", "2026-10-21", committee="TRAN"),
        make_finding("d", "2026-10-21", committee="XXX"),  # fallback chip
    ]
    html = render_dashboard(findings, "EP Space Monitor")
    assert "#D72828" in html  # SEDE -> Crimson
    assert "#A56CBD" in html  # ITRE -> Violet
    assert "#5C8BC3" in html  # TRAN -> Danube


def test_grouped_by_date_descending():
    findings = [
        make_finding("old", "2026-09-01"),
        make_finding("new", "2026-10-21"),
    ]
    html = render_dashboard(findings, "EP Space Monitor")
    assert html.index("2026-10-21") < html.index("2026-09-01")
    assert "Wednesday, 21 October 2026" in html


def test_html_escaping_of_titles():
    evil = make_finding("x", "2026-10-21", title="<script>alert('xss')</script>")
    html = render_dashboard([evil], "EP Space Monitor")
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_empty_findings_render_placeholder():
    html = render_dashboard([], "EP Space Monitor")
    assert "No space-relevant items flagged yet" in html


def test_write_dashboard(tmp_path):
    html = render_dashboard([make_finding("a", "2026-10-21")], "EP Space Monitor")
    target = tmp_path / "nested" / "dashboard.html"
    write_dashboard(html, target)
    assert target.exists()
    assert "EP Space Monitor" in target.read_text(encoding="utf-8")
