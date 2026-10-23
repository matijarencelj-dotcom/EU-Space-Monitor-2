"""Runner end-to-end tests with a fake API: no network, deterministic clock."""

from datetime import date

from ep_space_monitor.ep_api import ApiClient
from ep_space_monitor.runner import EPCommitteeMonitor, _months_between
from ep_space_monitor.state import Finding
from tests.conftest import TODAY, FakeApi, FakeMailer, write_test_config

MEETING_DAY = date(2026, 10, 21)   # Wednesday, one day before TODAY
FUTURE_DAY = date(2026, 11, 4)     # after TODAY (lookahead window)

ITRE_OJ_ITEMS = [
    {"number": 1, "title": "Adoption of agenda"},
    {
        "number": 6,
        "title": "Exchange of views with the Executive Director of the European Union Agency for the Space Programme (EUSPA)",
    },
    {"number": 8, "title": "Presentation of the Space Package", "documents": ["Communication on the EU Space Act"]},
]


def register_itre_agenda(api: FakeApi, day=MEETING_DAY, reference="ITRE(2026)1021_1"):
    api.register_events("ITRE", day.year, day.month, [(reference, day)])
    api.register_oj(reference, day, ITRE_OJ_ITEMS)


def make_monitor(config, api: FakeApi, mailer):
    client = ApiClient(api, delay=0.0)
    return EPCommitteeMonitor(config, client=client, mailer=mailer, clock=lambda: TODAY)


def months_in_default_window():
    # TODAY - 21d .. TODAY + 21d  ->  October and November 2026.
    return _months_between(date(2026, 10, 1), date(2026, 11, 12))


# -- committee agendas (eMeeting) ----------------------------------------------


def test_daily_run_detects_space_items_and_alerts(tmp_path):
    config = write_test_config(tmp_path, email_enabled=True)
    api = FakeApi()
    register_itre_agenda(api)
    mailer = FakeMailer()

    result = make_monitor(config, api, mailer).run()

    assert result.agendas_found == 1
    assert len(result.new_findings) == 2  # items 6 (EUSPA) and 8 (Space Package)
    assert {f.item_index for f in result.new_findings} == {6, 8}
    for finding in result.new_findings:
        assert finding.committee == "ITRE"
        assert finding.kind == "OJ"
        assert finding.kind_label == "agenda"
        assert finding.date == MEETING_DAY.isoformat()
        assert finding.url.startswith("https://www.europarl.europa.eu/meetdocs/")

    assert len(mailer.sent) == 1
    assert len(mailer.sent[0][0]) == 2

    dashboard = config.dashboard_path.read_text(encoding="utf-8")
    assert "EP Space Monitor" in dashboard
    assert "#E82275" in dashboard
    assert "ITRE" in dashboard


def test_second_run_never_repeats_alerts(tmp_path):
    config = write_test_config(tmp_path, email_enabled=True)
    api = FakeApi()
    register_itre_agenda(api)
    mailer = FakeMailer()

    first = make_monitor(config, api, mailer).run()
    assert len(first.new_findings) == 2

    second = make_monitor(config, api, mailer).run()
    assert second.new_findings == []
    assert second.agendas_found == 0
    assert len(mailer.sent) == 1  # still only the first alert


def test_future_meeting_without_agenda_is_watched(tmp_path):
    """Heads-up: a future meeting whose agenda is not published yet is pending."""
    config = write_test_config(tmp_path)
    api = FakeApi()
    api.register_events("ITRE", 2026, 11, [("ITRE(2026)1104_1", FUTURE_DAY)])
    # No OJ registered -> 204 (agenda not published yet).

    monitor = make_monitor(config, api, FakeMailer())
    result = monitor.run()

    assert result.agendas_found == 0
    assert result.new_findings == []
    assert result.pending_after == 1
    assert monitor.state.has_pending("ITRE", FUTURE_DAY)


def test_pending_meeting_resolves_when_agenda_appears(tmp_path):
    config = write_test_config(tmp_path, email_enabled=True)
    api = FakeApi()
    api.register_events("ITRE", 2026, 11, [("ITRE(2026)1104_1", FUTURE_DAY)])

    monitor = make_monitor(config, api, FakeMailer())
    run1 = monitor.run()
    assert run1.pending_after == 1

    # The agenda gets published between runs.
    api.register_oj("ITRE(2026)1104_1", FUTURE_DAY, ITRE_OJ_ITEMS)
    run2 = make_monitor(config, api, FakeMailer()).run()
    assert run2.agendas_found == 1
    assert run2.pending_after == 0
    assert len(run2.new_findings) == 2


def test_backfill_uses_year_archive(tmp_path):
    config = write_test_config(tmp_path)
    api = FakeApi()
    # One archive year with two published agendas, both inside the horizon.
    older = date(2026, 10, 2)
    api.register_archive("ITRE", 2026, [
        ("ITRE(2026)1002_1", older),
        ("ITRE(2026)1021_1", MEETING_DAY),
    ])
    api.register_oj("ITRE(2026)1002_1", older, ITRE_OJ_ITEMS)
    api.register_oj("ITRE(2026)1021_1", MEETING_DAY, ITRE_OJ_ITEMS)

    monitor = make_monitor(config, api, FakeMailer())
    result = monitor.backfill(months=1)

    assert result.agendas_found == 2
    assert monitor.state.is_processed("OJ:ITRE(2026)1021_1")


# -- parliamentary questions (Open Data) -----------------------------------------


def register_pq_year(api: FakeApi, ids_with_dates: list[tuple[str, str]]):
    identifiers = [ident for ident, _ in ids_with_dates]
    api.register_pq_page(2026, 0, 100, identifiers)
    for ident, day in ids_with_dates:
        api.register_pq(ident, date.fromisoformat(day), f"Question about {ident}")


def test_questions_first_pass_scores_space_questions(tmp_path):
    config = write_test_config(tmp_path, email_enabled=True)
    api = FakeApi()
    register_itre_agenda(api)
    api.register_pq_page(2026, 0, 100, ["E-10-2026-000101", "E-10-2026-000102", "E-10-2026-000103"])
    api.register_pq("E-10-2026-000101", date(2026, 10, 20), "Implementation of the Galileo programme")
    api.register_pq("E-10-2026-000102", date(2026, 10, 21), "Fisheries agreements in the Baltic Sea")
    api.register_pq("E-10-2026-000103", date(2026, 10, 22), "Copernicus budget for 2027")

    result = make_monitor(config, api, FakeMailer()).run()

    pq_findings = [f for f in result.new_findings if f.kind == "PQ"]
    assert len(pq_findings) == 2  # Galileo + Copernicus; fisheries below threshold
    assert all(f.committee == "EP" for f in pq_findings)
    assert all(f.kind_label == "question" for f in pq_findings)
    assert result.questions_checked == 3

    # All three were consumed: the cursor switches to steady state.
    import json as _json
    meta = _json.loads(config.state_path.read_text())["meta"]
    assert meta["pq_offset_2026"] == 3


def test_questions_second_run_uses_cursor(tmp_path):
    config = write_test_config(tmp_path)
    api = FakeApi()
    register_itre_agenda(api)
    api.register_pq_page(2026, 0, 100, ["E-10-2026-000101"])
    api.register_pq("E-10-2026-000101", date(2026, 10, 20), "Implementation of the Galileo programme")

    make_monitor(config, api, FakeMailer()).run()

    # A new question is appended: page from the cursor (offset 1) onwards.
    api.register_pq_page(2026, 1, 100, ["E-10-2026-000101", "E-10-2026-000102", "E-10-2026-000103"])
    api.register_pq_page(2026, 0, 100, ["E-10-2026-000101", "E-10-2026-000102", "E-10-2026-000103"])
    api.register_pq("E-10-2026-000102", date(2026, 10, 22), "Copernicus budget")
    api.register_pq("E-10-2026-000103", date(2026, 10, 22), "Rail infrastructure")

    result = make_monitor(config, api, FakeMailer()).run()
    pq_new = [f for f in result.new_findings if f.kind == "PQ"]
    assert {f.id for f in pq_new} == {"PQ:E-10-2026-000102"}
    assert result.questions_checked == 2  # 102 + 103, cursor skips 101


def test_questions_outside_window_seen_but_not_scored(tmp_path):
    config = write_test_config(tmp_path)
    api = FakeApi()
    register_itre_agenda(api)
    api.register_pq_page(2026, 0, 100, ["E-10-2026-000001", "E-10-2026-000002"])
    # April question: inside first-run history, outside daily window is
    # not checked here (first pass scores the whole history window).
    api.register_pq("E-10-2026-000001", date(2026, 4, 2), "Old Galileo question")
    api.register_pq("E-10-2026-000002", date(2026, 10, 22), "Tram infrastructure")

    result = make_monitor(config, api, FakeMailer()).run()
    pq = [f for f in result.new_findings if f.kind == "PQ"]
    # The old Galileo question is within the 180-day first-run window
    # (2026-04-22 onwards); April 2 is outside and only counted as seen.
    assert {f.id for f in pq} == set()


# -- plenary speeches (Open Data) --------------------------------------------


def test_plenary_search_flags_space_debates(tmp_path):
    config = write_test_config(tmp_path, email_enabled=True)
    api = FakeApi()
    register_itre_agenda(api)
    api.register_speeches(
        "space",
        [
            ("MTG-PL-2026-10-14-OTH-1", "2026-10-14", "Debate on the EU Space Strategy and the Space Act"),
            ("MTG-PL-2026-10-14-OTH-2", "2026-10-14", "Debate on fisheries quotas"),
        ],
    )
    api.register_speeches("galileo", [])

    result = make_monitor(config, api, FakeMailer()).run()
    ps = [f for f in result.new_findings if f.kind == "PS"]
    assert len(ps) == 1
    assert ps[0].kind_label == "plenary"
    assert ps[0].committee == "EP"
    assert ps[0].date == "2026-10-14"
    assert ps[0].url.endswith("speeches/MTG-PL-2026-10-14-OTH-1")
    assert result.speeches_checked == 2

    # Second run: nothing new (both hits already processed).
    second = make_monitor(config, api, FakeMailer()).run()
    assert [f for f in second.new_findings if f.kind == "PS"] == []


def test_eurlex_flags_council_documents(tmp_path):
    config = write_test_config(tmp_path, email_enabled=True)
    api = FakeApi()
    api.register_cellar(
        [
            # Council working-party document (consil view of a cellar work).
            ("http://publications.europa.eu/resource/cellar/aaaa", "consil:ST_4447", "2026-10-05",
             "Space Working Party meeting on the EU Space Programme"),
            # Same work stored under its CELEX alias — must be deduped away.
            ("http://publications.europa.eu/resource/cellar/aaaa", "celex:32026ST4447", "2026-10-05",
             "Space Working Party meeting on the EU Space Programme"),
            # Space act document within the window.
            ("http://publications.europa.eu/resource/cellar/bbbb", "celex:32026D2195", "2026-10-12",
             "Council Decision (CFSP) 2026/2195 on the EU Space Strategy"),
            # Noise: single weak keyword hit stays below threshold.
            ("http://publications.europa.eu/resource/cellar/cccc", "genpub:2026.75", "2026-10-10",
             "EPALE 2026 community storybook: the space between"),
            # Future-dated row: kept out by the local window filter.
            ("http://publications.europa.eu/resource/cellar/dddd", "celex:32026D9999", "2026-10-30",
             "Future-dated Council Decision on Galileo"),
        ]
    )

    result = make_monitor(config, api, FakeMailer()).run()
    lx = [f for f in result.new_findings if f.kind == "LX"]
    assert len(lx) == 2, [f.title for f in result.new_findings]
    assert all(f.kind_label == "eur-lex" for f in lx)
    assert all(f.committee == "EU" for f in lx)
    assert {f.id.split(":", 1)[0] for f in lx} == {"LX"}
    assert any("Working Party" in f.title for f in lx)
    assert not any("storybook" in f.title for f in lx)
    assert not any("Future-dated" in f.title for f in lx)
    # dedup: the consil/celex pair produced one finding, not two
    assert len([f for f in lx if "Working Party" in f.title]) == 1
    assert result.lex_checked == 3  # noise counted, future row filtered
    # urls point at EUR-Lex (celex) or the OP portal (other ids)
    assert all("eur-lex.europa.eu" in f.url or "op.europa.eu" in f.url for f in lx)

    # Second run: everything already processed.
    second = make_monitor(config, api, FakeMailer()).run()
    assert [f for f in second.new_findings if f.kind == "LX"] == []
    assert second.lex_checked == 0


# -- failure semantics: blocked responses are loud ------------------------------


def test_blocked_response_is_recorded_loudly(tmp_path):
    config = write_test_config(tmp_path)
    api = FakeApi()
    # October events blocked (202), November works.
    from ep_space_monitor.ep_api import events_url

    api.add(events_url("ITRE", 2026, 10, "en"), b"", status=202)
    api.register_events("ITRE", 2026, 11, [("ITRE(2026)1104_1", FUTURE_DAY)])

    result = make_monitor(config, api, FakeMailer()).run()
    assert result.errors, "a blocked response must be recorded"
    assert any("bot protection" in e for e in result.errors)
    assert result.pending_after == 1  # the November meeting is still watched


# -- misc ---------------------------------------------------------------------


def test_dry_run_sends_no_email(tmp_path):
    config = write_test_config(tmp_path, email_enabled=True)
    api = FakeApi()
    register_itre_agenda(api)
    mailer = FakeMailer()

    result = make_monitor(config, api, mailer).run(dry_run=True)
    assert len(result.new_findings) == 2
    assert mailer.sent == []


def test_findings_survive_state_reload(tmp_path):
    config = write_test_config(tmp_path)
    api = FakeApi()
    register_itre_agenda(api)

    make_monitor(config, api, FakeMailer()).run()
    reloaded = make_monitor(config, api, FakeMailer())
    assert len(reloaded.state.findings()) == 2


def test_sources_can_be_disabled(tmp_path):
    config = write_test_config(tmp_path, questions=False, plenary=False, eurlex=False)
    api = FakeApi()
    register_itre_agenda(api)
    api.register_pq_page(2026, 0, 100, ["E-10-2026-000001"])
    api.register_pq("E-10-2026-000001", date(2026, 10, 20), "Galileo funding question")

    result = make_monitor(config, api, FakeMailer()).run()
    assert result.questions_checked == 0
    assert result.speeches_checked == 0
    assert result.lex_checked == 0
    assert len(result.new_findings) == 2
    assert not any("parliamentary-questions" in url for url in api.calls)
    assert not any("/speeches?" in url for url in api.calls)
    assert not any("webapi/rdf/sparql" in url for url in api.calls)


def test_months_between_spans_year_boundary():
    months = _months_between(date(2026, 12, 15), date(2027, 1, 20))
    assert months == [(2026, 12), (2027, 1)]


def test_finding_kind_labels():
    f = Finding(
        id="x", committee="ITRE", kind="PQ", date="d", doc_number=0, url="u",
        item_index=0, title="t", excerpt="e", score=1.0, matched={},
        retrieved_at="now",
    )
    assert f.kind_label == "question"
    f2 = Finding(
        id="x", committee="EP", kind="PS", date="d", doc_number=0, url="u",
        item_index=0, title="t", excerpt="e", score=1.0, matched={},
        retrieved_at="now",
    )
    assert f2.kind_label == "plenary"
