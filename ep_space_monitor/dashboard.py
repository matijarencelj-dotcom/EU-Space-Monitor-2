"""ESPI-branded HTML dashboard generator.

Static, self-contained page listing findings by date and committee.
Branding follows the ESPI style guide: dark Woodsmoke/Gondola
backgrounds, Razzmatazz + Aquamarine accents, Raleway Black all-caps
headings (Gugi for the wordmark), committee chips in thematic stream
colours.
"""

from __future__ import annotations

import html
from pathlib import Path

RAZZMATAZZ = "#E82275"
AQUAMARINE = "#8ACCD0"
WOODSMOKE = "#0C1112"
GONDOLA = "#240D16"

# Committee chip colours by ESPI thematic stream (style-guide alternates).
STREAM_COLORS = {
    "SEDE": "#D72828",  # Security & Defence -> Crimson
    "ITRE": "#A56CBD",   # Industry, Innovation -> Violet
    "TRAN": "#5C8BC3",   # Space as an Asset -> Danube
    "BUDG": "#F6A800",   # Budget -> Sun
}
DEFAULT_CHIP = AQUAMARINE

BASE_CSS = f"""
:root {{
  --bg: {WOODSMOKE}; --panel: {GONDOLA}; --razzmatazz: {RAZZMATAZZ};
  --aquamarine: {AQUAMARINE}; --text: #EAE6E9; --muted: #9A9298;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  background: var(--bg); color: var(--text);
  font-family: "Tenorite", "Segoe UI", Arial, sans-serif;
  line-height: 1.55; min-height: 100vh;
}}
.wrap {{ max-width: 1080px; margin: 0 auto; padding: 32px 24px 64px; }}
h1, h2, h3, .stat-num {{ font-family: "Raleway", "Segoe UI", Arial, sans-serif; font-weight: 900; text-transform: uppercase; letter-spacing: .04em; }}
header {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: 14px; border-bottom: 2px solid var(--razzmatazz); padding-bottom: 18px; margin-bottom: 8px; }}
.wordmark {{ font-family: "Gugi", "Raleway", sans-serif; color: var(--aquamarine); font-size: 1.9rem; }}
h1 {{ font-size: 1.55rem; }}
h1 .accent {{ color: var(--razzmatazz); }}
.subtitle {{ color: var(--muted); font-size: .95rem; margin: 8px 0 24px; }}
.stats {{ display: flex; flex-wrap: wrap; gap: 14px; margin-bottom: 26px; }}
.stat {{ background: var(--panel); border-left: 3px solid var(--aquamarine); padding: 12px 18px; min-width: 130px; }}
.stat-num {{ font-size: 1.5rem; color: var(--aquamarine); }}
.stat-label {{ color: var(--muted); font-size: .78rem; text-transform: uppercase; letter-spacing: .08em; }}
.filters {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 26px; }}
.chip {{ border: 1px solid var(--aquamarine); background: transparent; color: var(--aquamarine); padding: 5px 14px; border-radius: 999px; cursor: pointer; font-size: .8rem; font-family: inherit; }}
.chip.active, .chip:hover {{ background: var(--aquamarine); color: var(--bg); }}
input#search {{ background: var(--panel); border: 1px solid var(--panel); border-bottom: 1px solid var(--aquamarine); color: var(--text); padding: 7px 12px; min-width: 220px; font-family: inherit; font-size: .85rem; outline: none; }}
.date-section {{ margin-bottom: 30px; }}
h2 {{ font-size: 1.05rem; color: var(--aquamarine); border-bottom: 1px solid var(--gondola-border, #2b1a24); padding-bottom: 6px; margin-bottom: 14px; }}
.date-section > h2 span {{ color: var(--muted); font-weight: 400; text-transform: none; letter-spacing: 0; font-family: "Tenorite", Arial, sans-serif; font-size: .8rem; }}
.card {{ background: var(--panel); border: 1px solid #2b1a24; border-left: 3px solid var(--razzmatazz); padding: 16px 18px; margin-bottom: 12px; }}
.card.hidden {{ display: none; }}
.card-top {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 8px; }}
.kind {{ font-size: .68rem; letter-spacing: .12em; padding: 2px 8px; border-radius: 3px; text-transform: uppercase; font-weight: 700; }}
.kind.OJ {{ background: var(--aquamarine); color: var(--bg); }}
.kind.PQ {{ background: var(--razzmatazz); color: #fff; }}
.kind.PS {{ background: #F6A800; color: #0C1112; }}
.kind.LX {{ background: #5C8BC3; color: #fff; }}
.kind.FT {{ background: #BADE45; color: #0C1112; }}
.kind.PV {{ background: var(--razzmatazz); color: #fff; }}
.committee {{ font-size: .7rem; font-weight: 900; padding: 2px 10px; border-radius: 999px; letter-spacing: .1em; }}
.item-no {{ color: var(--muted); font-size: .78rem; }}
.score {{ color: var(--razzmatazz); font-weight: 700; font-size: .78rem; margin-left: auto; }}
.card h3 {{ font-size: .95rem; text-transform: none; letter-spacing: 0; margin-bottom: 6px; font-weight: 700; }}
.keywords {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }}
.kw {{ font-size: .72rem; border: 1px solid var(--razzmatazz); color: var(--razzmatazz); padding: 1px 9px; border-radius: 999px; }}
.excerpt {{ color: var(--muted); font-size: .85rem; font-style: italic; margin-bottom: 10px; }}
a.src {{ color: var(--aquamarine); font-size: .78rem; text-decoration: none; border-bottom: 1px dotted var(--aquamarine); }}
a.src:hover {{ color: #fff; border-color: #fff; }}
.empty {{ color: var(--muted); font-style: italic; padding: 30px 0; text-align: center; }}
footer {{ margin-top: 40px; color: var(--muted); font-size: .75rem; border-top: 1px solid #2b1a24; padding-top: 14px; }}
footer b {{ color: var(--aquamarine); }}
"""

_FILTER_JS = """
(function () {
  var active = "ALL";
  var cards = document.querySelectorAll("[data-committee]");
  var buttons = document.querySelectorAll(".chip[data-committee]");
  var search = document.getElementById("search");
  function apply() {
    var q = (search.value || "").toLowerCase();
    cards.forEach(function (card) {
      var okCommittee = active === "ALL" || card.dataset.committee === active;
      var okText = !q || card.textContent.toLowerCase().indexOf(q) !== -1;
      card.classList.toggle("hidden", !(okCommittee && okText));
    });
    document.querySelectorAll(".date-section").forEach(function (section) {
      var visible = section.querySelectorAll(".card:not(.hidden)").length > 0;
      section.style.display = visible ? "" : "none";
    });
  }
  buttons.forEach(function (btn) {
    btn.addEventListener("click", function () {
      active = btn.dataset.committee;
      buttons.forEach(function (b) { b.classList.toggle("active", b === btn); });
      apply();
    });
  });
  search.addEventListener("input", apply);
})();
"""


def _fmt_date(iso: str) -> str:
    from datetime import date

    try:
        d = date.fromisoformat(iso)
        return d.strftime("%A, %d %B %Y")
    except ValueError:
        return iso


def _committee_chip(committee: str) -> str:
    color = STREAM_COLORS.get(committee.upper(), DEFAULT_CHIP)
    return (
        f'<span class="committee" style="background:{color}1a;'
        f'color:{color};border:1px solid {color};">{html.escape(committee.upper())}</span>'
    )


def render_dashboard(findings, site_name: str = "EP Space Monitor", generated_at: str = "") -> str:
    """Render the findings into a self-contained HTML page."""
    sorted_findings = sorted(findings, key=lambda f: (f.date, f.committee, f.doc_number, f.item_index), reverse=True)

    by_date: dict[str, list] = {}
    for finding in sorted_findings:
        by_date.setdefault(finding.date, []).append(finding)

    committees = sorted({f.committee.upper() for f in sorted_findings})
    n_agenda = sum(1 for f in sorted_findings if f.kind == "OJ")
    n_questions = sum(1 for f in sorted_findings if f.kind == "PQ")
    n_plenary = sum(1 for f in sorted_findings if f.kind == "PS")
    n_lex = sum(1 for f in sorted_findings if f.kind == "LX")
    n_ft = sum(1 for f in sorted_findings if f.kind == "FT")

    sections: list[str] = []
    for iso_date, items in by_date.items():
        cards: list[str] = []
        for f in items:
            keywords_html = "".join(f'<span class="kw">{html.escape(term)}</span>' for term in f.matched)
            item_no = (
                f'<span class="item-no">item {f.item_index} · doc #{f.doc_number}</span>'
                if f.kind == "OJ" else ""
            )
            cards.append(
                f'<div class="card" data-committee="{html.escape(f.committee.upper())}">'
                f'<div class="card-top">'
                f'<span class="kind {html.escape(f.kind)}">{html.escape(f.kind_label)}</span>'
                f'{_committee_chip(f.committee)}'
                f'{item_no}'
                f'<span class="score">score {f.score:.1f}</span>'
                f"</div>"
                f"<h3>{html.escape(f.title)}</h3>"
                f'<div class="keywords">{keywords_html}</div>'
                f'<p class="excerpt">{html.escape(f.excerpt)}</p>'
                f'<a class="src" href="{html.escape(f.url, quote=True)}" target="_blank" rel="noopener">View source document ↗</a>'
                f"</div>"
            )
        sections.append(
            f'<div class="date-section" data-date="{html.escape(iso_date)}">'
            f"<h2>{html.escape(_fmt_date(iso_date))} <span>— {len(items)} space-relevant item(s)</span></h2>"
            + "".join(cards)
            + "</div>"
        )

    chips = (
        '<button class="chip active" data-committee="ALL">All committees</button>'
        + "".join(
            f'<button class="chip" data-committee="{html.escape(c)}">{html.escape(c)}</button>'
            for c in committees
        )
    )

    body = (
        f'<div class="wrap">'
        f'<header><span class="wordmark">ESPI</span><h1>{html.escape(site_name)} <span class="accent">//</span> EU space sector watch</h1></header>'
        f'<p class="subtitle">EP committee agendas, parliamentary questions, plenary debates, '
        f"EUR-Lex/Council documents and EU funding calls flagged for space-sector relevance: EU Space Programme, Galileo, "
        f"Copernicus, IRIS², EU Space Act, launchers, space defence &amp; security, satellites, ESA, EUSPA.</p>"
        f'<div class="stats">'
        f'<div class="stat"><div class="stat-num">{len(sorted_findings)}</div><div class="stat-label">flagged items</div></div>'
        f'<div class="stat"><div class="stat-num">{n_agenda}</div><div class="stat-label">from agendas</div></div>'
        f'<div class="stat"><div class="stat-num">{n_questions}</div><div class="stat-label">from questions</div></div>'
        f'<div class="stat"><div class="stat-num">{n_plenary}</div><div class="stat-label">from plenary</div></div>'
        f'<div class="stat"><div class="stat-num">{n_lex}</div><div class="stat-label">from eur-lex</div></div>'
        f'<div class="stat"><div class="stat-num">{n_ft}</div><div class="stat-label">from funding</div></div>'
        f'<div class="stat"><div class="stat-num">{len(committees)}</div><div class="stat-label">committees</div></div>'
        f"</div>"
        f'<div class="filters">{chips}<input id="search" type="search" placeholder="Filter items… (e.g. Galileo)"></div>'
        + ("".join(sections) if sections else '<p class="empty">No space-relevant items flagged yet.</p>')
        + f"</div>"
        f'<footer>Generated by <b>EP Space Monitor</b> for ESPI · European Space Policy Institute'
        + (f" · last run: {html.escape(generated_at)}" if generated_at else "")
        + "</footer>"
    )

    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(site_name)} — ESPI</title>\n"
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        '<link href="https://fonts.googleapis.com/css2?family=Raleway:wght@700;900&family=Gugi&display=swap" rel="stylesheet">\n'
        f"<style>{BASE_CSS}</style>\n</head>\n<body>\n{body}\n<script>{_FILTER_JS}</script>\n</body>\n</html>\n"
    )


def write_dashboard(html_text: str, output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_text, encoding="utf-8")
    return output_path
