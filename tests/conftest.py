"""Shared fixtures and helpers for the EP Space Monitor test suite."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

from ep_space_monitor.config import Config, load_config  # noqa: E402
from ep_space_monitor.detection import load_keywords  # noqa: E402
from ep_space_monitor.ep_api import (  # noqa: E402
    events_url,
    archive_url,
    oj_url,
    pq_detail_url,
    pq_list_url,
    speeches_url,
    cellar_url,
)

KEYWORDS_PATH = ROOT / "keywords.toml"

# A fixed "today" so the runner tests are deterministic (a Thursday).
TODAY = date(2026, 10, 22)

EMEETING_HOST = "https://emeeting.europarl.europa.eu/emeeting/plmrep/"
OPENDATA_HOST = "https://data.europarl.europa.eu/api/v2/"


def load_test_keywords():
    return load_keywords(KEYWORDS_PATH)


def to_epoch_ms(day: date, hour: int = 9) -> int:
    return int(datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc).timestamp() * 1000)


class FakeApi:
    """URL getter that serves canned API responses.

    Unknown URLs return HTTP 204 (no content) — like the real APIs do for
    empty result sets and unpublished agendas.
    """

    def __init__(self):
        self.urls: dict[str, tuple[int, bytes, str]] = {}
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: int):
        self.calls.append(url)
        entry = self.urls.get(url)
        if entry is None:
            return 204, b"", ""
        status, content, ctype = entry
        if callable(content):
            status, content, ctype = content()
        return status, content, ctype

    def add(self, url: str, payload, content_type: str = "application/json", status: int = 200):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode("utf-8")
        elif isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.urls[url] = (status, payload, content_type)

    # -- eMeeting registration helpers -----------------------------------

    def register_events(self, organ: str, year: int, month: int, meetings: list[tuple[str, date]]):
        """Register an eventsByMonth response; meetings = [(reference, day)]."""
        payload = [
            {
                "meetingReference": reference,
                "start": to_epoch_ms(day),
                "end": to_epoch_ms(day, 12),
                "jointOrgans": [reference.split("(")[0]],
            }
            for reference, day in meetings
        ]
        self.add(events_url(organ, year, month, "en"), payload)

    def register_archive(self, organ: str, year: int, entries: list[tuple[str, date]]):
        """Register an agendaArchive response; entries = [(reference, day)]."""
        months: dict[int, list] = {}
        for reference, day in entries:
            months.setdefault(day.month, []).append(
                {
                    "date": f"{day.day:02d}.{day.month:02d}.{day.year}",
                    "ojReference": reference,
                    "month": f"{day.month:02d}",
                    "year": str(day.year),
                }
            )
        payload = [
            {"year": year, "months": [{"month": name, "ojs": ojs} for name, ojs in sorted(months.items())]}
        ]
        self.add(archive_url(organ, year, "en"), payload)

    def register_oj(
        self,
        reference: str,
        day: date,
        items: list[dict],
        en_pdf: str = "https://www.europarl.europa.eu/meetdocs/2024_2029/plmrep/COMMITTEES/ITRE/OJ/2026/10-08/1351971EN.pdf",
    ):
        """Register an OJ agenda; items = [{"number", "title", "documents"?}]."""
        payload_items = []
        for item in items:
            entry = {"number": item.get("number"), "title": item.get("title", ""), "documentSets": []}
            for doc_title in item.get("documents", []):
                entry["documentSets"].append(
                    {
                        "type": "OTH",
                        "documents": [{"geproCode": "PR", "title": doc_title, "documentLinks": []}],
                    }
                )
            payload_items.append(entry)
        payload = {
            "meetingReference": reference,
            "date": to_epoch_ms(day),
            "items": payload_items,
            "ojDocument": {
                "geproCode": "OJ",
                "title": "Agenda",
                "documentLinks": [
                    {"type": "pdf", "languages": [{"code": "EN", "url": en_pdf}]}
                ],
            },
        }
        self.add(oj_url(reference, "en"), payload)

    def register_unpublished_oj(self, reference: str):
        """Register the 200-with-empty-body response for a missing agenda."""
        self.add(oj_url(reference, "en"), b"", status=200)

    # -- Open Data registration helpers ------------------------------------

    def register_pq_page(self, year: int, offset: int, limit: int, identifiers: list[str]):
        payload = {"data": [{"id": f"eli/dl/doc/{i}", "identifier": i} for i in identifiers]}
        self.add(pq_list_url(year, offset, limit), payload)

    def register_pq(
        self,
        identifier: str,
        day: date,
        title: str,
        alt_title: str = "",
        dist_path: str | None = None,
    ):
        en: dict = {"title": {"en": title}}
        if alt_title:
            en["title_alternative"] = {"en": alt_title}
        if dist_path:
            en["is_embodied_by"] = [
                {
                    "id": f"{identifier}/en/pdf",
                    "type": "Manifestation",
                    "is_exemplified_by": dist_path,
                }
            ]
        payload = {
            "data": [
                {
                    "id": f"eli/dl/doc/{identifier}",
                    "type": "Work",
                    "identifier": identifier,
                    "document_date": day.isoformat(),
                    "work_type": "def/ep-document-types/QUESTION_WRITTEN",
                    "is_realized_by": [
                        {
                            "id": f"eli/dl/doc/{identifier}/en",
                            "type": "Expression",
                            "language": "http://publications.europa.eu/resource/authority/language/ENG",
                            **en,
                        }
                    ],
                }
            ]
        }
        self.add(pq_detail_url(identifier), payload)

    def register_speeches(
        self,
        query: str,
        hits: list[tuple[str, str, str]],
        limit: int = 25,
        offset: int = 0,
    ):
        """Register a speeches title-search page; hits = [(activity_id, day, label)]."""
        payload = {
            "data": [
                {
                    "id": f"eli/dl/event/{activity_id}",
                    "type": "Activity",
                    "activity_id": activity_id,
                    "activity_date": day,
                    "activity_label": {"en": label},
                }
                for activity_id, day, label in hits
            ]
        }
        self.add(speeches_url(query, limit, offset), payload)

    def register_cellar(self, docs, terms=("space", "galileo"), date_from="2026-10-08", limit=100):
        """Register a CELLAR SPARQL response; docs = [(work, doc_id, day, title)]."""
        payload = {
            "results": {
                "bindings": [
                    {
                        "work": {"value": work},
                        "id": {"value": doc_id},
                        "date": {"value": day},
                        "title": {"value": title},
                    }
                    for work, doc_id, day, title in docs
                ]
            }
        }
        self.add(cellar_url(list(terms), date_from, limit), payload)



def make_docx_bytes(paragraphs: list[str]) -> bytes:
    from docx import Document

    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_pdf_bytes(lines: list[str]) -> bytes:
    """Build a minimal one-page PDF containing the given lines."""

    def esc(s: str) -> str:
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    ops = ["BT", "/F1 12 Tf", "14 TL", "40 780 Td"]
    for line in lines:
        ops.append(f"({esc(line)}) Tj T*")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1", "replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + "\nendobj\n".encode()
    xref_pos = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
        + str(xref_pos).encode()
        + b"\n%%EOF\n"
    )
    return out


def write_test_config(
    tmp_path: Path,
    *,
    committees=("ITRE",),
    email_enabled=False,
    lookback=21,
    lookahead=21,
    questions=True,
    plenary=True,
    eurlex=True,
) -> Config:
    committees_list = ", ".join(f'"{c}"' for c in committees)
    text = f"""
[general]
language = "EN"
data_dir = "{tmp_path / "data"}"
keywords_file = "{KEYWORDS_PATH}"

[monitor]
committees = [{committees_list}]
agenda_lookback_days = {lookback}
agenda_lookahead_days = {lookahead}
pending_max_age_days = 30
questions_enabled = {str(questions).lower()}
questions_lookback_days = 30
questions_first_run_days = 180
questions_max_new = 400
questions_page_size = 100
questions_max_pages = 60
plenary_enabled = {str(plenary).lower()}
plenary_lookback_days = 14
plenary_queries = ["space", "galileo"]
plenary_limit_per_query = 25
eurlex_enabled = {str(eurlex).lower()}
eurlex_lookback_days = 14
eurlex_queries = ["space", "galileo"]
eurlex_limit = 100
request_delay_seconds = 0.0
request_timeout_seconds = 5
max_retries = 0
first_run_backfill = false
backfill_months = 6

[dashboard]
output_path = "{tmp_path / "data" / "dashboard.html"}"
site_name = "EP Space Monitor"

[email]
enabled = {str(email_enabled).lower()}
to = ["matija.rencelj@espi.eu"]
from = "ep-space-monitor@espi.eu"
subject_prefix = "EP Space Monitor"
smtp_host = "smtp-relay.brevo.com"
smtp_port = 587
smtp_user = ""
smtp_password_env = "EP_SPACE_MONITOR_SMTP_PASSWORD"

[network]
allowlist = ["emeeting.europarl.europa.eu", "data.europarl.europa.eu"]
user_agent = "test-agent"
"""
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


class FakeMailer:
    def __init__(self):
        self.sent: list[tuple[list, str]] = []

    def send_findings(self, findings, run_date: str):
        self.sent.append((findings, run_date))

    def send(self, subject, html_body, plain_body):
        self.sent.append((subject, html_body, plain_body))


@pytest.fixture
def agenda_html() -> str:
    return (FIXTURES / "agenda_itre.html").read_text(encoding="utf-8")


@pytest.fixture
def keywords():
    return load_test_keywords()
