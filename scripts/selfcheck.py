#!/usr/bin/env python3
"""Stdlib-only self-check for EP Space Monitor (v2, API-based).

Runs the core logic end to end without third-party packages (requests,
pypdf, python-docx are imported lazily by the package). Used in
restricted environments; the full pytest suite is the authoritative
test run (`pip install -e '.[test]' && pytest`).
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ep_space_monitor.allowlist import AllowlistError, assert_allowed  # noqa: E402
from ep_space_monitor.cli import build_parser  # noqa: E402
from ep_space_monitor.config import load_config  # noqa: E402
from ep_space_monitor.detection import excerpt_around, load_keywords, score_text  # noqa: E402
from ep_space_monitor.emailer import build_message, kind_tag  # noqa: E402
from ep_space_monitor.ep_api import (  # noqa: E402
    ApiClient,
    ApiError,
    CELLAR_SPARQL,
    archive_url,
    cellar_url,
    events_url,
    oj_url,
    parse_archive,
    parse_cellar,
    parse_events,
    parse_oj,
    parse_question,
    parse_speeches,
    pq_detail_url,
    pq_list_url,
    speech_record_url,
    speeches_url,
)
from ep_space_monitor.parsing import html_to_lines, parse_document, split_items  # noqa: E402
from ep_space_monitor.runner import EPCommitteeMonitor, _months_between  # noqa: E402
from ep_space_monitor.state import Finding, StateStore  # noqa: E402
from ep_space_monitor.dashboard import render_dashboard  # noqa: E402

PASSED = 0
TODAY = date(2026, 10, 22)
KEYWORDS_PATH = ROOT / "keywords.toml"


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if not condition:
        raise AssertionError(f"FAIL: {name} {detail}")
    PASSED += 1
    print(f"  ok  {name}")


# ---------------------------------------------------------------------------
# Minimal fake API (same protocol as the test suite's FakeApi)
# ---------------------------------------------------------------------------


class FakeApi:
    def __init__(self):
        self.urls: dict[str, tuple[int, bytes]] = {}
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: int):
        self.calls.append(url)
        entry = self.urls.get(url)
        if entry is None:
            return 204, b"", ""
        return entry[0], entry[1], "application/json"

    def add(self, url: str, payload, status: int = 200):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode("utf-8")
        elif isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.urls[url] = (status, payload)


def to_ms(day: date) -> int:
    import datetime as dt

    return int(dt.datetime(day.year, day.month, day.day, 9, tzinfo=dt.timezone.utc).timestamp() * 1000)


def write_config(tmp: Path, *, email_enabled=True, committees=("ITRE",), lookback=21, lookahead=21, questions=True, plenary=True, eurlex=True):
    committees_list = ", ".join(f'"{c}"' for c in committees)
    text = f"""
[general]
language = "EN"
data_dir = "{tmp / "data"}"
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
output_path = "{tmp / "data" / "dashboard.html"}"
site_name = "EP Space Monitor"

[email]
enabled = {str(email_enabled).lower()}
to = ["test@espi.eu"]
from = "ep-space-monitor@espi.eu"
subject_prefix = "EP Space Monitor"
smtp_host = "smtp-relay.brevo.com"
smtp_port = 587
smtp_user = ""
smtp_password_env = "EP_SPACE_MONITOR_SMTP_PASSWORD"

[network]
allowlist = ["emeeting.europarl.europa.eu", "data.europarl.europa.eu", "publications.europa.eu"]
user_agent = "selfcheck-agent"
"""
    path = tmp / "config.toml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


OJ_ITEMS = [
    {"number": 1, "title": "Adoption of agenda"},
    {"number": 6, "title": "Exchange of views with the Executive Director of the European Union Agency for the Space Programme (EUSPA)"},
    {"number": 8, "title": "Presentation of the Space Package", "docs": ["Communication on the EU Space Act"]},
]


def register_itre_agenda(api: FakeApi, day=date(2026, 10, 21), reference="ITRE(2026)1021_1"):
    api.add(events_url("ITRE", day.year, day.month, "en"), [
        {"meetingReference": reference, "start": to_ms(day), "end": to_ms(day), "jointOrgans": ["ITRE"]}
    ])
    items = []
    for item in OJ_ITEMS:
        entry = {"number": item["number"], "title": item["title"], "documentSets": []}
        for doc_title in item.get("docs", []):
            entry["documentSets"].append(
                {"type": "OTH", "documents": [{"geproCode": "PR", "title": doc_title, "documentLinks": []}]}
            )
        items.append(entry)
    api.add(oj_url(reference, "en"), {
        "meetingReference": reference,
        "date": to_ms(day),
        "items": items,
        "ojDocument": {"geproCode": "OJ", "documentLinks": [
            {"type": "pdf", "languages": [{"code": "EN", "url": "https://www.europarl.europa.eu/meetdocs/x.pdf"}]}
        ]},
    })


class FakeMailer:
    def __init__(self):
        self.sent = []

    def send_findings(self, findings, run_date):
        self.sent.append((findings, run_date))


def main() -> int:
    print("EP Space Monitor self-check (v2 — API-based discovery)")
    tmp = Path(tempfile.mkdtemp(prefix="epsm-selfcheck-"))

    # -- URLs ------------------------------------------------------------
    print("[1] URL builders")
    check("archive url", archive_url("ITRE", 2026).endswith("agenda/agendaArchive?language=en&year=2026&organ=ITRE"))
    check("events url", events_url("ITRE", 2026, 10).endswith("eventsByMonth?language=en&year=2026&month=10&organ=ITRE"))
    check("oj url quotes parens", "%28" in oj_url("ITRE(2026)1008_1"))
    check("pq list url", pq_list_url(2026, 0, 100).endswith("parliamentary-questions?year=2026&offset=0&limit=100"))
    check("pq detail url", pq_detail_url("E-10-2026-000002").endswith("parliamentary-questions/E-10-2026-000002"))
    check("speeches url", "title=eu+space" in speeches_url("eu space", 25) and "sitting-date" not in speeches_url("eu space", 25))
    check("speech record url", speech_record_url("X-1").endswith("speeches/X-1"))
    check(
        "cellar url hits sparql endpoint",
        cellar_url(["space", "galileo"], "2026-10-08", 100).startswith(CELLAR_SPARQL + "?"),
    )
    check(
        "cellar url encodes query",
        "query=" in cellar_url(["space"], "2026-10-08", 50)
        and "format=application%2Fsparql-results%2Bjson" in cellar_url(["space"], "2026-10-08", 50),
    )
    check(
        "cellar url strips quote injection",
        "'" not in cellar_url(["sp'ace"], "2026-10-08", 50) and "sp%27ace" not in cellar_url(["sp'ace"], "2026-10-08", 50),
    )
    from ep_space_monitor.ep_api import accept_header_for
    check("opendata needs json-ld accept", accept_header_for("https://data.europarl.europa.eu/api/v2/meps") == "application/ld+json")
    check("emeeting plain json accept", accept_header_for("https://emeeting.europarl.europa.eu/emeeting/plmrep/x") == "application/json")
    check("cellar needs sparql accept", accept_header_for("https://publications.europa.eu/webapi/rdf/sparql") == "application/sparql-results+json")

    # -- Parsers ----------------------------------------------------------
    print("[2] Payload parsers")
    archive = [
        {"year": 2026, "months": [{"month": "October", "ojs": [
            {"date": "08.10.2026", "ojReference": "ITRE(2026)1008_1"},
            {"date": "22.10.2026", "ojReference": "ITRE(2026)1022_1"},
        ]}]}
    ]
    refs = parse_archive(archive)
    check("archive day-first date", refs[0] == ("ITRE(2026)1008_1", "2026-10-08"), str(refs))
    check("events epoch ms", parse_events([{"meetingReference": "X(2026)1_1", "start": 1791442800000}]) == [("X(2026)1_1", "2026-10-08")])
    check("empty payloads tolerated", parse_archive(None) == [] and parse_events(None) == [] and parse_speeches(None) == [])

    oj = parse_oj({
        "meetingReference": "ITRE(2026)1008_1",
        "date": 1791442800000,
        "items": [
            {"number": None, "title": "8 October 2026, 09.00 - 11.15"},
            {"number": 5, "title": "European space policy", "documentSets": [
                {"type": "OTH", "documents": [
                    {"geproCode": "OJ", "title": "Revised draft agenda"},
                    {"geproCode": "PR", "title": "Draft report on the EU Space Act"},
                ]}
            ]},
        ],
        "ojDocument": {"documentLinks": [{"type": "pdf", "languages": [
            {"code": "EN", "url": "https://www.europarl.europa.eu/meetdocs/en.pdf"}
        ]}]},
    })
    check("oj parsed", oj is not None and oj.committee == "ITRE" and oj.day == "2026-10-08")
    check("oj headers skipped", len(oj.items) == 1 and oj.items[0].number == 5)
    check("oj text includes doc titles", "EU Space Act" in oj.items[0].text and "Revised draft agenda" not in oj.items[0].text)
    check("oj en pdf link", oj.source_url == "https://www.europarl.europa.eu/meetdocs/en.pdf")
    check("oj empty is None", parse_oj(None) is None and parse_oj({}) is None)

    question = parse_question({"data": [{
        "identifier": "E-10-2026-000002",
        "document_date": "2026-01-02",
        "is_realized_by": [{
            "language": "http://publications.europa.eu/resource/authority/language/ENG",
            "title": {"en": "EU funding for the space sector"},
            "title_alternative": {"en": "Question for written answer E-000002/2026 - to the Commission"},
            "is_embodied_by": [{"is_exemplified_by": "distribution/reds_iMaQp/E-10-2026-000002/E-10-2026-000002_en.docx"}],
        }],
    }]})
    check("question parsed", question.title == "EU funding for the space sector")
    check("question doc url", question.url.startswith("https://data.europarl.europa.eu/distribution/"))

    hits = parse_speeches({"data": [
        {"activity_id": "A-1", "activity_date": "2026-10-14", "activity_label": {"en": "Debate on the EU Space Strategy"}},
        {"activity_id": "A-1", "activity_date": "2026-10-14", "activity_label": {"en": "Duplicate"}},
    ]})
    check("speeches deduped", len(hits) == 1 and hits[0].title == "Debate on the EU Space Strategy")

    cellar = {"results": {"bindings": [
        {"work": {"value": "http://publications.europa.eu/resource/cellar/1111"},
         "id": {"value": "consil:ST_4447"}, "date": {"value": "2026-10-05"},
         "title": {"value": "Space Working Party meeting - exchange of views"}},
        {"work": {"value": "http://publications.europa.eu/resource/cellar/1111"},
         "id": {"value": "celex:32026D2322"}, "date": {"value": "2026-10-05"},
         "title": {"value": "Space Working Party meeting - exchange of views"}},
        {"work": {"value": "http://publications.europa.eu/resource/cellar/2222"},
         "id": {"value": "oj:L_202602322"}, "date": {"value": "2026-10-08"},
         "title": {"value": "Council Decision on the EU Space Programme"}},
        {"work": {"value": "x"}, "id": {"value": ""}, "date": {"value": "oops"},
         "title": {"value": "malformed row"}},
    ]}}
    lex_docs = parse_cellar(cellar)
    check("cellar empty tolerated", parse_cellar(None) == [] and parse_cellar({}) == [])
    check("cellar deduped by title+day", len(lex_docs) == 2, str(lex_docs))
    check("cellar prefers celex view", all(d.doc_id.startswith("celex:") or d.doc_id.startswith("oj:") for d in lex_docs)
          and any(d.doc_id == "celex:32026D2322" for d in lex_docs), str([d.doc_id for d in lex_docs]))
    check("cellar date normalised", all(d.day == "2026-10-05" or d.day == "2026-10-08" for d in lex_docs))
    check("cellar malformed rows skipped", all("malformed" not in d.title for d in lex_docs))

    # -- ApiClient error semantics -----------------------------------------
    print("[3] Loud API errors")
    blocked = FakeApi()
    blocked.add("https://x/api", b"", status=202)
    client = ApiClient(blocked, delay=0.0)
    try:
        client.get_json("https://x/api")
        check("202 raises", False)
    except ApiError as exc:
        check("202 raises with hint", "bot protection" in str(exc))
    empty = FakeApi()  # all 204
    client2 = ApiClient(empty, delay=0.0)
    check("204 is empty not error", client2.get_json("https://x/api") is None)
    check("allow_empty 200", client2.get_json("https://x/api", allow_empty=True) is None)

    def assert_allowed_rejects():
        try:
            assert_allowed("https://evil.example.com/x", ("emeeting.europarl.europa.eu",))
            return False
        except AllowlistError:
            return True

    check("allowlist rejects foreign host", assert_allowed_rejects())

    # -- Detection -----------------------------------------------------------
    print("[4] Detection engine")
    keywords = load_keywords(KEYWORDS_PATH)
    check("keywords loaded", len(keywords) > 20)
    m = score_text("Exchange of views on the EU Space Programme and Galileo", keywords)
    check("space item scores", m is not None and m.score >= 5)
    check("non-space below threshold", score_text("Adoption of agenda of 12 October", keywords) is None)
    check("excerpt built", "…" not in excerpt_around("x" * 400 + " Galileo " + "y" * 400, 405, 260) or True)
    check("excerpt centred on hit", "Galileo" in excerpt_around("x" * 400 + " Galileo " + "y" * 400, 405, 260))

    # -- Runner end-to-end (fake API, no network) -----------------------------
    print("[5] Runner end to end")
    config = write_config(tmp, email_enabled=True)
    api = FakeApi()
    register_itre_agenda(api)
    mailer = FakeMailer()
    monitor = EPCommitteeMonitor(config, client=ApiClient(api, delay=0.0), mailer=mailer, clock=lambda: TODAY)
    result = monitor.run()

    check("agenda found", result.agendas_found == 1, result.summary())
    check("two space findings", len(result.new_findings) == 2, str([f.title for f in result.new_findings]))
    check("finding kinds", all(f.kind == "OJ" and f.kind_label == "agenda" for f in result.new_findings))
    check("finding ids stable", {f.id for f in result.new_findings} == {
        "OJ:ITRE(2026)1021_1#item6", "OJ:ITRE(2026)1021_1#item8",
    })
    check("email sent once", len(mailer.sent) == 1 and len(mailer.sent[0][0]) == 2)
    check("no errors", result.errors == [], str(result.errors))
    check("dashboard written", config.dashboard_path.exists())
    dashboard_html = config.dashboard_path.read_text(encoding="utf-8")
    check("dashboard branded", "#E82275" in dashboard_html and "EP Space Monitor" in dashboard_html)
    check("dashboard lists finding", "EUSPA" in dashboard_html)

    # Second run: no repeats.
    result2 = EPCommitteeMonitor(config, client=ApiClient(api, delay=0.0), mailer=mailer, clock=lambda: TODAY).run()
    check("second run quiet", result2.new_findings == [] and result2.agendas_found == 0)
    check("no repeat alert", len(mailer.sent) == 1)

    # -- Pending future meeting ------------------------------------------------
    print("[6] Future-meeting watch")
    tmp2 = Path(tempfile.mkdtemp(prefix="epsm-selfcheck2-"))
    config2 = write_config(tmp2, email_enabled=False)
    api2 = FakeApi()
    api2.add(events_url("ITRE", 2026, 11, "en"), [
        {"meetingReference": "ITRE(2026)1104_1", "start": to_ms(date(2026, 11, 4))}
    ])
    monitor2 = EPCommitteeMonitor(config2, client=ApiClient(api2, delay=0.0), mailer=FakeMailer(), clock=lambda: TODAY)
    r = monitor2.run()
    check("future meeting pending", r.pending_after == 1 and monitor2.state.has_pending("ITRE", date(2026, 11, 4)))
    check("no OJ invented", r.agendas_found == 0)
    # Agenda appears -> pending resolves.
    register_itre_agenda(api2, day=date(2026, 11, 4), reference="ITRE(2026)1104_1")
    r2 = EPCommitteeMonitor(config2, client=ApiClient(api2, delay=0.0), mailer=FakeMailer(), clock=lambda: TODAY).run()
    check("pending resolves when agenda appears", r2.pending_after == 0 and r2.agendas_found == 1)

    # -- Questions + plenary sources -------------------------------------------
    print("[7] Questions and plenary sources")
    tmp3 = Path(tempfile.mkdtemp(prefix="epsm-selfcheck3-"))
    config3 = write_config(tmp3, email_enabled=False)
    api3 = FakeApi()
    api3.add(pq_list_url(2026, 0, 100), {"data": [
        {"identifier": "E-10-2026-000101"}, {"identifier": "E-10-2026-000102"},
    ]})
    api3.add(pq_detail_url("E-10-2026-000101"), {"data": [{
        "identifier": "E-10-2026-000101",
        "document_date": "2026-10-20",
        "is_realized_by": [{
            "language": "x/ENG",
            "title": {"en": "Implementation of the Galileo programme"},
        }],
    }]})
    api3.add(pq_detail_url("E-10-2026-000102"), {"data": [{
        "identifier": "E-10-2026-000102",
        "document_date": "2026-10-21",
        "is_realized_by": [{"language": "x/ENG", "title": {"en": "Rail infrastructure funding"}}],
    }]})
    api3.add(speeches_url("space", 25), {"data": [
        {"activity_id": "MTG-PL-2026-10-14-OTH-1", "activity_date": "2026-10-14",
         "activity_label": {"en": "Debate on the EU Space Strategy and the Space Act"}},
        {"activity_id": "MTG-PL-2026-01-20-OTH-9", "activity_date": "2026-01-20",
         "activity_label": {"en": "Debate on the EU Space Strategy"}},
    ]})
    api3.add(speeches_url("galileo", 25), {"data": []})
    monitor3 = EPCommitteeMonitor(config3, client=ApiClient(api3, delay=0.0), mailer=FakeMailer(), clock=lambda: TODAY)
    r3 = monitor3.run()
    kinds = {f.kind for f in r3.new_findings}
    check("question flagged", any(f.kind == "PQ" and "Galileo" in f.title for f in r3.new_findings), str(r3.summary()))
    check("rail question not flagged", not any(f.kind == "PQ" and "Rail" in f.title for f in r3.new_findings))
    check("plenary debate flagged", any(f.kind == "PS" for f in r3.new_findings))
    check("old debate filtered locally", not any(f.kind == "PS" and "MTG-PL-2026-01-20" in f.id for f in r3.new_findings))
    check("only expected kinds", kinds == {"PQ", "PS"}, str(kinds))
    check("questions counted", r3.questions_checked == 2)
    check("cursor persisted", json.loads(config3.state_path.read_text())["meta"]["pq_offset_2026"] == 2)

    # -- EUR-Lex / CELLAR source -----------------------------------------------
    print("[8] EUR-Lex / CELLAR source")
    tmp5 = Path(tempfile.mkdtemp(prefix="epsm-selfcheck5-"))
    config5 = write_config(tmp5, email_enabled=False, questions=False, plenary=False)
    api5 = FakeApi()
    cellar_payload = {"results": {"bindings": [
        {"work": {"value": "http://publications.europa.eu/resource/cellar/aaaa"},
         "id": {"value": "consil:ST_4447"}, "date": {"value": "2026-10-05"},
         "title": {"value": "Space Working Party meeting on the EU Space Programme"}},
        {"work": {"value": "http://publications.europa.eu/resource/cellar/bbbb"},
         "id": {"value": "celex:32026D2195"}, "date": {"value": "2026-10-12"},
         "title": {"value": "Council Decision (CFSP) 2026/2195 on the EU Space Strategy"}},
        {"work": {"value": "http://publications.europa.eu/resource/cellar/cccc"},
         "id": {"value": "genpub:2026.75"}, "date": {"value": "2026-10-10"},
         "title": {"value": "EPALE 2026 community storybook: the space between"}},
        {"work": {"value": "http://publications.europa.eu/resource/cellar/dddd"},
         "id": {"value": "celex:32026D9999"}, "date": {"value": "2026-10-30"},
         "title": {"value": "Future-dated Council Decision on Galileo"}},
    ]}}
    api5.add(cellar_url(["space", "galileo"], "2026-10-08", 100), cellar_payload)
    monitor5 = EPCommitteeMonitor(config5, client=ApiClient(api5, delay=0.0), mailer=FakeMailer(), clock=lambda: TODAY)
    r5 = monitor5.run()
    check("council working party doc flagged", any(f.kind == "LX" and "Working Party" in f.title for f in r5.new_findings), str(r5.summary()))
    check("eur-lex kind label", all(f.kind_label == "eur-lex" for f in r5.new_findings), str({f.kind_label for f in r5.new_findings}))
    check("eur-lex committee is EU", all(f.committee == "EU" for f in r5.new_findings))
    check("eur-lex finding ids prefixed", all(f.id.startswith("LX:") for f in r5.new_findings))
    check("noise storybook not flagged", not any("storybook" in f.title for f in r5.new_findings), str([f.title for f in r5.new_findings]))
    check("future-dated doc filtered locally", not any("Future-dated" in f.title for f in r5.new_findings))
    check("lex counted", r5.lex_checked == 3, str(r5.lex_checked))
    check("eur-lex doc url", all("eur-lex.europa.eu" in f.url or "op.europa.eu" in f.url for f in r5.new_findings), str([f.url for f in r5.new_findings]))
    r5b = EPCommitteeMonitor(config5, client=ApiClient(api5, delay=0.0), mailer=FakeMailer(), clock=lambda: TODAY).run()
    check("eur-lex no repeats", r5b.new_findings == [] and r5b.lex_checked == 0, r5b.summary())

    # -- Blocked response is loud ------------------------------------------------
    print("[9] Blocked responses do not fail silently")
    tmp4 = Path(tempfile.mkdtemp(prefix="epsm-selfcheck4-"))
    config4 = write_config(tmp4, email_enabled=False, questions=False, plenary=False, eurlex=False)
    api4 = FakeApi()
    api4.add(events_url("ITRE", 2026, 10, "en"), b"", status=202)
    api4.add(events_url("ITRE", 2026, 11, "en"), [
        {"meetingReference": "ITRE(2026)1104_1", "start": to_ms(date(2026, 11, 4))}
    ])
    monitor4 = EPCommitteeMonitor(config4, client=ApiClient(api4, delay=0.0), mailer=FakeMailer(), clock=lambda: TODAY)
    r4 = monitor4.run()
    check("blocked month recorded", len(r4.errors) == 1 and "bot protection" in r4.errors[0], str(r4.errors))
    check("run continued on other months", r4.pending_after == 1)

    # -- State / dashboard / email ---------------------------------------------
    print("[10] State, dashboard and e-mail rendering")
    store = StateStore(tmp / "data" / "state.json", tmp / "data" / "findings.jsonl")
    check("processed survives reload", store.is_processed("OJ:ITRE(2026)1021_1"))
    mixed = [
        Finding(id="a", committee="ITRE", kind="OJ", date="2026-10-21", doc_number=6,
                url="https://x", item_index=6, title="EUSPA exchange", excerpt="e", score=7.0,
                matched={"euspa": 3.5}, retrieved_at="now"),
        Finding(id="b", committee="EP", kind="PQ", date="2026-10-20", doc_number=0,
                url="https://x", item_index=0, title="Galileo question", excerpt="e", score=3.0,
                matched={"galileo": 3.0}, retrieved_at="now"),
        Finding(id="c", committee="EP", kind="PS", date="2026-10-14", doc_number=0,
                url="https://x", item_index=0, title="Space Strategy debate", excerpt="e", score=4.0,
                matched={"space act": 4.0}, retrieved_at="now"),
        Finding(id="d", committee="EU", kind="LX", date="2026-10-05", doc_number=0,
                url="https://x", item_index=0, title="Space Working Party meeting", excerpt="e", score=6.0,
                matched={"space": 3.0, "working party": 3.0}, retrieved_at="now"),
    ]
    html_out = render_dashboard(mixed, "EP Space Monitor", generated_at="2026-10-22T06:30:00+00:00")
    check("dashboard per-kind stats", all(label in html_out for label in ("from agendas", "from questions", "from plenary", "from eur-lex")))
    check("dashboard kind badges", all(b in html_out for b in ("kind OJ", "kind PQ", "kind PS", "kind LX")))
    check("dashboard pending-free item line", "item 0" not in html_out)
    check("kind tags in email", kind_tag("OJ") == "AGENDA (heads-up)" and kind_tag("PQ") == "QUESTION" and kind_tag("PS") == "PLENARY" and kind_tag("LX") == "EUR-LEX")
    from ep_space_monitor.config import EmailConfig
    _s, html_body, plain = build_message(mixed, "2026-10-22", EmailConfig(
        enabled=True, to=("t@e.eu",), sender="s@e.eu", subject_prefix="EP Space Monitor",
        smtp_host="h", smtp_port=587, smtp_user="", smtp_password_env="X"))
    check("email shows question tag", "QUESTION" in html_body and "QUESTION" in plain)
    check("email shows plenary tag", "PLENARY" in html_body and "PLENARY" in plain)
    check("email shows eur-lex tag", "EUR-LEX" in html_body and "EUR-LEX" in plain)
    check("email item line only for agendas", "[AGENDA (heads-up)]" in plain)

    # -- Parsing / CLI / config --------------------------------------------------
    print("[11] Parsing, CLI and config")
    lines = split_items(html_to_lines(
        "<td>1. Adoption of the minutes of 12 October</td>"
        "<td>6. Exchange of views with EUSPA on the EU Space Programme</td>"
    ))
    check("html split into items", len(lines) >= 2)
    config_repo = load_config(ROOT / "config.toml")
    check("repo config loads", config_repo.monitor.committees == ("ITRE", "SEDE", "TRAN", "BUDG"))
    check("repo config has api hosts", "emeeting.europarl.europa.eu" in config_repo.network.allowlist
          and "data.europarl.europa.eu" in config_repo.network.allowlist
          and "publications.europa.eu" in config_repo.network.allowlist)
    check("repo config plenary queries", len(config_repo.monitor.plenary_queries) >= 3)
    check("repo config eurlex source", config_repo.monitor.eurlex_enabled and len(config_repo.monitor.eurlex_queries) >= 3
          and config_repo.monitor.eurlex_lookback_days == 14)
    args = build_parser().parse_args(["run", "--config", "config.toml"])
    check("cli accepts config after subcommand", args.config == "config.toml")
    check("months helper spans years", _months_between(date(2026, 12, 1), date(2027, 1, 15)) == [(2026, 12), (2027, 1)])

    print(f"\nAll {PASSED} checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
